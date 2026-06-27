"""Email/password registration and JWT login."""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps_auth import get_current_admin_user, get_current_user
from app.config import Settings, get_settings
from app.db.models import AccessKeyRow
from app.db.models_user import UserEmailVerificationRow, UserRow
from app.db.session import db_session_dep
from app.services.activation_codes import redeem_activation_code
from app.services.auth_rate_limit import (
    clear_login_failure,
    is_login_locked,
    record_login_failure,
    register_rate_limited,
)
from app.services.email_sender import EmailSendError, is_email_configured, send_verification_email
from app.services.jwt_tokens import create_access_token
from app.services.membership import membership_public_fields, resolve_v3_access
from app.services.passwords import hash_password, verify_password

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


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
    turnstile_token: Optional[str] = Field(default=None, max_length=4096)


class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    turnstile_token: Optional[str] = Field(default=None, max_length=4096)


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    display_name: Optional[str]
    role: str
    created_at: Optional[datetime]
    email_verified: bool
    membership: dict[str, object] = Field(default_factory=dict)


class UserAccessKeySummary(BaseModel):
    total: int = 0
    active: int = 0
    revoked: int = 0
    latest_key_prefix: Optional[str] = None
    latest_key_type: Optional[str] = None
    latest_status: Optional[str] = None
    latest_days_remaining: Optional[int] = None
    latest_expires_at: Optional[datetime] = None
    latest_last_used_at: Optional[datetime] = None


class AdminUserPublic(UserPublic):
    access_keys: UserAccessKeySummary = Field(default_factory=UserAccessKeySummary)


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


class ResendVerificationBody(BaseModel):
    email: EmailStr
    turnstile_token: Optional[str] = Field(default=None, max_length=4096)


class ResendVerificationResponse(BaseModel):
    verification_required: bool = True
    verification_expires_at: datetime
    verification_code: Optional[str] = None


class AdminUserPatchBody(BaseModel):
    role: Literal["user", "admin", "disabled"]


class RedeemCodeBody(BaseModel):
    code: str = Field(min_length=8, max_length=64)


class RedeemCodeResponse(BaseModel):
    success: bool = True
    user: UserPublic
    membership_expires_at: datetime


def _to_public(row: UserRow) -> UserPublic:
    access = resolve_v3_access(row)
    return UserPublic(
        id=row.id,
        email=row.email,
        display_name=row.display_name,
        role=row.role,
        created_at=row.created_at,
        email_verified=bool(row.email_verified),
        membership=membership_public_fields(access),
    )


def _days_remaining(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    delta = dt - datetime.now(timezone.utc)
    return max(0, int(delta.total_seconds() // 86400))


def _access_key_summary_for(row: UserRow, keys: list[AccessKeyRow]) -> UserAccessKeySummary:
    matched = [
        key
        for key in keys
        if key.bound_user_id == row.id or (key.bound_email or "").strip().lower() == row.email.strip().lower()
    ]
    latest = sorted(
        matched,
        key=lambda key: key.last_used_at or key.activated_at or key.created_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[0] if matched else None
    return UserAccessKeySummary(
        total=len(matched),
        active=sum(1 for key in matched if key.status == "active"),
        revoked=sum(1 for key in matched if key.status == "revoked"),
        latest_key_prefix=latest.key_prefix if latest else None,
        latest_key_type=latest.key_type if latest else None,
        latest_status=latest.status if latest else None,
        latest_days_remaining=_days_remaining(latest.expires_at) if latest else None,
        latest_expires_at=latest.expires_at if latest else None,
        latest_last_used_at=latest.last_used_at if latest else None,
    )


def _to_admin_public(row: UserRow, keys: list[AccessKeyRow]) -> AdminUserPublic:
    base = _to_public(row).model_dump()
    return AdminUserPublic(**base, access_keys=_access_key_summary_for(row, keys))


def _hash_verification_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _generate_verification_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _invalidate_pending_verifications(session: Session, user_id: str) -> None:
    now = datetime.now(timezone.utc)
    pending = session.execute(
        select(UserEmailVerificationRow).where(
            UserEmailVerificationRow.user_id == user_id,
            UserEmailVerificationRow.consumed_at.is_(None),
        )
    ).scalars().all()
    for row in pending:
        row.consumed_at = now
        session.add(row)


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
    _invalidate_pending_verifications(session, user.id)
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


def _deliver_verification_email(
    *,
    settings: Settings,
    to_email: str,
    code: str,
    expires_at: datetime,
) -> None:
    if not is_email_configured(settings):
        if settings.auth_verification_debug_expose_code:
            logger.warning(
                "Email delivery not configured; code only exposed in debug response for %s",
                to_email,
            )
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "email_not_configured",
                "message": "邮件服务未配置，暂时无法发送验证码。",
            },
        )
    try:
        send_verification_email(
            to_email=to_email,
            code=code,
            expires_at=expires_at,
            settings=settings,
        )
    except EmailSendError as exc:
        logger.exception("Verification email failed to=%s", to_email)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "email_send_failed",
                "message": "验证码邮件发送失败，请稍后重试。",
            },
        ) from exc


def _register_response(
    *,
    row: UserRow,
    verification: UserEmailVerificationRow,
    code: str,
    settings: Settings,
) -> RegisterResponse:
    return RegisterResponse(
        user=_to_public(row),
        verification_expires_at=verification.expires_at,
        verification_code=code if settings.auth_verification_debug_expose_code else None,
    )


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


def _verify_turnstile_or_raise(
    *,
    settings: Settings,
    token: Optional[str],
    ip: str,
    action: str,
) -> None:
    if not settings.turnstile_enabled:
        return

    token = (token or "").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "turnstile_required", "message": "请先完成人机验证。"},
        )

    secret = settings.turnstile_secret_key.strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "turnstile_not_configured", "message": "人机验证服务未配置。"},
        )

    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(
                TURNSTILE_VERIFY_URL,
                data={
                    "secret": secret,
                    "response": token,
                    "remoteip": ip,
                },
            )
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Turnstile verification request failed action=%s ip=%s error=%s", action, ip, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "turnstile_unavailable", "message": "人机验证服务暂不可用，请稍后再试。"},
        ) from exc

    if not isinstance(payload, dict) or payload.get("success") is not True:
        logger.info(
            "Turnstile verification failed action=%s ip=%s error_codes=%s",
            action,
            ip,
            payload.get("error-codes") if isinstance(payload, dict) else None,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "turnstile_failed", "message": "人机验证失败，请重试。"},
        )

    returned_action = str(payload.get("action") or "").strip()
    if returned_action and returned_action != action:
        logger.warning(
            "Turnstile action mismatch expected=%s got=%s ip=%s",
            action,
            returned_action,
            ip,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "turnstile_failed", "message": "人机验证失败，请重试。"},
        )


@router.post("/register", response_model=RegisterResponse)
async def register(
    body: RegisterBody,
    request: Request,
    session: Session = Depends(db_session_dep),
) -> RegisterResponse:
    settings = get_settings()
    ip = _client_ip(request)
    _verify_turnstile_or_raise(settings=settings, token=body.turnstile_token, ip=ip, action="register")
    if register_rate_limited(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "rate_limited", "message": "注册过于频繁，请稍后再试。"},
        )

    email = _norm_email(str(body.email))
    _validate_password_strength(body.password)

    exists = session.execute(select(UserRow).where(UserRow.email == email)).scalar_one_or_none()
    if exists is not None:
        if exists.email_verified:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "email_taken", "message": "该邮箱已注册。"},
            )
        if not verify_password(body.password, exists.password_hash):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "email_taken", "message": "该邮箱已注册。"},
            )
        verification, code = _issue_verification_code(session=session, user=exists, settings=settings)
        try:
            _deliver_verification_email(
                settings=settings,
                to_email=email,
                code=code,
                expires_at=verification.expires_at,
            )
        except HTTPException:
            session.rollback()
            raise
        session.commit()
        session.refresh(exists)
        session.refresh(verification)
        logger.info(
            "Verification re-issued for unverified user id=%s email=%s ip=%s verification_id=%s",
            exists.id,
            email,
            ip,
            verification.id,
        )
        return _register_response(row=exists, verification=verification, code=code, settings=settings)

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
    try:
        _deliver_verification_email(
            settings=settings,
            to_email=email,
            code=code,
            expires_at=verification.expires_at,
        )
    except HTTPException:
        session.rollback()
        raise
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
    return _register_response(row=row, verification=verification, code=code, settings=settings)


@router.post("/register/resend", response_model=ResendVerificationResponse)
async def register_resend(
    body: ResendVerificationBody,
    request: Request,
    session: Session = Depends(db_session_dep),
) -> ResendVerificationResponse:
    settings = get_settings()
    ip = _client_ip(request)
    _verify_turnstile_or_raise(settings=settings, token=body.turnstile_token, ip=ip, action="resend")
    if register_rate_limited(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "rate_limited", "message": "请求过于频繁，请稍后再试。"},
        )

    email = _norm_email(str(body.email))
    row = session.execute(select(UserRow).where(UserRow.email == email)).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "user_not_found", "message": "该邮箱尚未注册。"},
        )
    if row.email_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "already_verified", "message": "该邮箱已完成验证，请直接登录。"},
        )

    verification, code = _issue_verification_code(session=session, user=row, settings=settings)
    try:
        _deliver_verification_email(
            settings=settings,
            to_email=email,
            code=code,
            expires_at=verification.expires_at,
        )
    except HTTPException:
        session.rollback()
        raise
    session.commit()
    session.refresh(verification)
    logger.info(
        "Verification resent user_id=%s email=%s ip=%s verification_id=%s",
        row.id,
        email,
        ip,
        verification.id,
    )
    return ResendVerificationResponse(
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
            detail={"code": "verification_not_found", "message": "验证码已失效，请重新发送验证码。"},
        )

    now = datetime.now(timezone.utc)
    max_attempts = max(1, int(get_settings().auth_verification_max_attempts))
    if verify.attempts >= max_attempts:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "too_many_attempts", "message": "验证码尝试次数过多，请重新发送验证码。"},
        )
    if _ensure_utc(verify.expires_at) < now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "code_expired", "message": "验证码已过期，请重新发送验证码。"},
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
    request: Request,
    session: Session = Depends(db_session_dep),
) -> TokenResponse:
    settings = get_settings()
    ip = _client_ip(request)
    _verify_turnstile_or_raise(settings=settings, token=body.turnstile_token, ip=ip, action="login")

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


@router.post("/redeem", response_model=RedeemCodeResponse)
async def redeem_code(
    body: RedeemCodeBody,
    request: Request,
    user: Annotated[UserRow, Depends(get_current_user)],
    session: Session = Depends(db_session_dep),
) -> RedeemCodeResponse:
    updated = redeem_activation_code(
        session,
        user=user,
        raw_code=body.code,
        client_ip=_client_ip(request),
    )
    expires = updated.membership_expires_at
    if expires is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "membership_missing", "message": "会员状态更新失败。"},
        )
    return RedeemCodeResponse(
        user=_to_public(updated),
        membership_expires_at=expires,
    )


@router.post("/logout")
async def logout(user: Annotated[UserRow, Depends(get_current_user)]) -> dict[str, bool]:
    logger.debug("Logout user id=%s", user.id)
    return {"success": True}


@router.get("/admin/users", response_model=list[AdminUserPublic])
async def admin_list_users(
    _: Annotated[UserRow, Depends(get_current_admin_user)],
    session: Session = Depends(db_session_dep),
) -> list[AdminUserPublic]:
    rows = session.execute(select(UserRow).order_by(UserRow.created_at.desc())).scalars().all()
    access_keys = session.execute(select(AccessKeyRow)).scalars().all()
    return [_to_admin_public(r, list(access_keys)) for r in rows]


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
