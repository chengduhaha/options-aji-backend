"""Email/password registration and JWT login."""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps_auth import get_current_admin_user, get_current_user
from app.config import Settings, get_settings
from app.db.models_user import UserEmailVerificationRow, UserRow
from app.db.session import db_session_dep
from app.services.auth_rate_limit import (
    clear_login_failure,
    is_login_locked,
    record_login_failure,
    register_rate_limited,
)
from app.services.jwt_tokens import create_access_token
from app.services.passwords import hash_password, verify_password

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _norm_email(email: str) -> str:
    return email.strip().lower()


def _client_ip(request: Request) -> str:
    """Prefer CDN / edge forwarded headers so Vercel → VPS proxy does not collapse all users to one IP."""
    for header in (
        "x-forwarded-for",
        "x-real-ip",
        "x-vercel-forwarded-for",
        "cf-connecting-ip",
    ):
        raw = request.headers.get(header)
        if raw:
            part = raw.split(",")[0].strip()
            if part:
                return part
    if request.client:
        return request.client.host
    return "unknown"


def _admin_emails(settings: Settings) -> set[str]:
    return {e.strip().lower() for e in settings.auth_admin_emails.split(",") if e.strip()}


def _validate_password_strength(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "weak_password", "message": "密码至少 8 位。"},
        )
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "weak_password",
                "message": "密码需同时包含字母与数字。",
            },
        )


class RegisterBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: Optional[str] = Field(default=None, max_length=128)


class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    display_name: Optional[str]
    role: str
    created_at: Optional[datetime]
    email_verified: bool


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserPublic


class RegisterResponse(BaseModel):
    user: UserPublic
    verification_required: bool = True
    verification_expires_at: datetime
    verification_code: Optional[str] = None


class RegisterVerifyBody(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=32)


class AdminUserPatchBody(BaseModel):
    role: Literal["user", "admin", "disabled"]


def _to_public(row: UserRow) -> UserPublic:
    return UserPublic(
        id=row.id,
        email=row.email,
        display_name=row.display_name,
        role=row.role,
        created_at=row.created_at,
        email_verified=bool(row.email_verified),
    )


def _hash_verification_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _generate_verification_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _issue_verification_code(
    *,
    session: Session,
    user: UserRow,
    settings: Settings,
) -> tuple[UserEmailVerificationRow, str]:
    if not user.id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "user_not_ready", "message": "用户创建失败，请稍后重试。"},
        )
    now = datetime.now(timezone.utc)
    ttl_seconds = max(60, int(settings.auth_verification_code_ttl_seconds))
    code = _generate_verification_code()
    verification = UserEmailVerificationRow(
        user_id=user.id,
        email=user.email,
        code_hash=_hash_verification_code(code),
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    session.add(verification)
    return verification, code


def _latest_pending_verification(session: Session, user_id: str) -> Optional[UserEmailVerificationRow]:
    return (
        session.execute(
            select(UserEmailVerificationRow)
            .where(
                UserEmailVerificationRow.user_id == user_id,
                UserEmailVerificationRow.consumed_at.is_(None),
            )
            .order_by(UserEmailVerificationRow.created_at.desc())
        )
        .scalars()
        .first()
    )


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@router.post("/register", response_model=RegisterResponse)
async def register(
    body: RegisterBody,
    request: Request,
    session: Session = Depends(db_session_dep),
) -> RegisterResponse:
    settings = get_settings()
    ip = _client_ip(request)
    if register_rate_limited(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "rate_limited", "message": "注册过于频繁，请稍后再试。"},
        )

    email = _norm_email(str(body.email))
    _validate_password_strength(body.password)

    exists = session.execute(select(UserRow).where(UserRow.email == email)).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "email_taken", "message": "该邮箱已注册。"},
        )

    role = "admin" if email in _admin_emails(settings) else "user"
    row = UserRow(
        email=email,
        password_hash=hash_password(body.password),
        display_name=body.display_name.strip() if body.display_name else None,
        role=role,
        email_verified=False,
    )
    session.add(row)
    session.flush()
    verification, code = _issue_verification_code(session=session, user=row, settings=settings)
    session.commit()
    session.refresh(row)
    session.refresh(verification)

    logger.info(
        "User registered id=%s email=%s role=%s ip=%s verification_id=%s",
        row.id,
        email,
        role,
        ip,
        verification.id,
    )
    return RegisterResponse(
        user=_to_public(row),
        verification_expires_at=verification.expires_at,
        verification_code=code if settings.auth_verification_debug_expose_code else None,
    )


@router.post("/register/verify", response_model=TokenResponse)
async def register_verify(
    body: RegisterVerifyBody,
    session: Session = Depends(db_session_dep),
) -> TokenResponse:
    email = _norm_email(str(body.email))
    code = body.code.strip()
    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_code", "message": "验证码不能为空。"},
        )

    row = session.execute(select(UserRow).where(UserRow.email == email)).scalar_one_or_none()
    if row is None:
        logger.warning("Verify failed: user not found email=%s", email)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "user_not_found", "message": "用户不存在。"},
        )
    if row.role == "disabled":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "account_disabled", "message": "账号已禁用。"},
        )
    if row.email_verified:
        token = create_access_token(user_id=row.id, email=row.email, role=row.role)
        return TokenResponse(access_token=token, user=_to_public(row))

    verify = _latest_pending_verification(session, row.id)
    if verify is None:
        logger.warning("Verify failed: no active verification user_id=%s email=%s", row.id, email)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "verification_not_found", "message": "验证码已失效，请重新注册。"},
        )

    now = datetime.now(timezone.utc)
    max_attempts = max(1, int(get_settings().auth_verification_max_attempts))
    if verify.attempts >= max_attempts:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "too_many_attempts", "message": "验证码尝试次数过多，请重新注册。"},
        )
    if _ensure_utc(verify.expires_at) < now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "code_expired", "message": "验证码已过期，请重新注册。"},
        )

    if verify.code_hash != _hash_verification_code(code):
        verify.attempts += 1
        session.add(verify)
        session.commit()
        logger.info("Verify failed: wrong code email=%s attempts=%s", email, verify.attempts)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_code", "message": "验证码错误。"},
        )

    verify.consumed_at = now
    row.email_verified = True
    row.last_login_at = now
    session.add(verify)
    session.add(row)
    session.commit()
    session.refresh(row)
    logger.info("Email verified user_id=%s email=%s", row.id, row.email)
    token = create_access_token(user_id=row.id, email=row.email, role=row.role)
    return TokenResponse(access_token=token, user=_to_public(row))


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginBody,
    session: Session = Depends(db_session_dep),
) -> TokenResponse:
    email = _norm_email(str(body.email))
    if is_login_locked(email):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "locked", "message": "登录失败次数过多，请 15 分钟后再试。"},
        )

    row = session.execute(select(UserRow).where(UserRow.email == email)).scalar_one_or_none()
    if row is None or not verify_password(body.password, row.password_hash):
        record_login_failure(email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_credentials", "message": "邮箱或密码错误。"},
        )

    if row.role == "disabled":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "account_disabled", "message": "账号已禁用。"},
        )
    if not row.email_verified and _latest_pending_verification(session, row.id) is not None:
        logger.info("Login blocked pending verification email=%s", email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "email_not_verified", "message": "邮箱尚未验证，请先完成验证码验证。"},
        )

    clear_login_failure(email)
    row.last_login_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()

    token = create_access_token(user_id=row.id, email=row.email, role=row.role)
    return TokenResponse(access_token=token, user=_to_public(row))


@router.get("/me", response_model=UserPublic)
async def me(user: Annotated[UserRow, Depends(get_current_user)]) -> UserPublic:
    return _to_public(user)


@router.post("/logout")
async def logout(user: Annotated[UserRow, Depends(get_current_user)]) -> dict[str, bool]:
    logger.debug("Logout user id=%s", user.id)
    return {"success": True}


@router.get("/admin/users", response_model=list[UserPublic])
async def admin_list_users(
    _: Annotated[UserRow, Depends(get_current_admin_user)],
    session: Session = Depends(db_session_dep),
) -> list[UserPublic]:
    rows = session.execute(select(UserRow).order_by(UserRow.created_at.desc())).scalars().all()
    return [_to_public(r) for r in rows]


@router.patch("/admin/users/{user_id}", response_model=UserPublic)
async def admin_patch_user(
    user_id: str,
    body: AdminUserPatchBody,
    _: Annotated[UserRow, Depends(get_current_admin_user)],
    session: Session = Depends(db_session_dep),
) -> UserPublic:
    row = session.get(UserRow, user_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "用户不存在。"},
        )
    row.role = body.role
    session.add(row)
    session.commit()
    session.refresh(row)
    logger.info("Admin patched user id=%s role=%s", user_id, body.role)
    return _to_public(row)
