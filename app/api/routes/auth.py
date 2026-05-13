"""Email/password registration and JWT login."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps_auth import get_current_admin_user, get_current_user
from app.config import Settings, get_settings
from app.db.models_user import UserRow
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


@router.post("/register", response_model=TokenResponse)
async def register(
    body: RegisterBody,
    request: Request,
    session: Session = Depends(db_session_dep),
) -> TokenResponse:
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
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    logger.info("User registered id=%s email=%s role=%s ip=%s", row.id, email, role, ip)
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
