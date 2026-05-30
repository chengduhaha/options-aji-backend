"""Manual access-key entitlement helpers for early-stage paid MVP access."""

from __future__ import annotations

import hashlib
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AccessKeyRow
from app.db.models_user import UserRow

VALID_KEY_TYPES = {"trial", "paid", "internal"}
VALID_STATUSES = {"active", "revoked"}


@dataclass(frozen=True)
class IssuedAccessKey:
    raw_key: str
    key_prefix: str
    key_type: str
    duration_days: int


@dataclass(frozen=True)
class AccessKeyGrant:
    key_prefix: str
    key_type: str
    expires_at: datetime
    usage_count: int


@dataclass(frozen=True)
class AccessKeyStatus:
    valid: bool
    status: str
    key_prefix: str
    key_type: str
    activated_at: Optional[datetime]
    expires_at: Optional[datetime]
    days_remaining: Optional[int]
    usage_count: int
    is_activated: bool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def hash_access_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.strip().encode("utf-8")).hexdigest()


def _prefix_for(raw_key: str) -> str:
    return raw_key[:24]


def _new_raw_key(key_type: str) -> str:
    return f"aji_{key_type}_{secrets.token_urlsafe(24)}"


def days_remaining_for(expires_at: Optional[datetime], *, now: Optional[datetime] = None) -> Optional[int]:
    if expires_at is None:
        return None
    ref = now or _now()
    delta = _aware(expires_at) - ref
    if delta.total_seconds() <= 0:
        return 0
    return int(math.ceil(delta.total_seconds() / 86400))


def create_access_key(
    session: Session,
    *,
    key_type: str,
    duration_days: int,
    note: str = "",
    max_devices: int = 1,
) -> IssuedAccessKey:
    normalized_type = key_type.strip().lower()
    if normalized_type not in VALID_KEY_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_key_type", "message": "key_type must be trial, paid, or internal."},
        )
    days = max(1, int(duration_days))
    devices = max(1, int(max_devices))
    for _ in range(5):
        raw_key = _new_raw_key(normalized_type)
        row = AccessKeyRow(
            key_hash=hash_access_key(raw_key),
            key_prefix=_prefix_for(raw_key),
            key_type=normalized_type,
            status="active",
            duration_days=days,
            max_devices=devices,
            note=note.strip(),
        )
        session.add(row)
        try:
            session.commit()
            return IssuedAccessKey(
                raw_key=raw_key,
                key_prefix=row.key_prefix,
                key_type=row.key_type,
                duration_days=row.duration_days,
            )
        except Exception:
            session.rollback()
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"code": "key_generation_failed", "message": "Could not generate a unique access key."},
    )


def _reject(status_code: int, code: str, message: str) -> None:
    raise HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _status_from_row(row: AccessKeyRow, *, now: Optional[datetime] = None) -> AccessKeyStatus:
    ref = now or _now()
    is_activated = row.activated_at is not None
    expires = _aware(row.expires_at) if row.expires_at else None
    if row.status != "active":
        key_status = "revoked"
        valid = False
    elif is_activated and expires is not None and expires <= ref:
        key_status = "expired"
        valid = False
    elif is_activated:
        key_status = "active"
        valid = True
    else:
        key_status = "pending"
        valid = True

    return AccessKeyStatus(
        valid=valid,
        status=key_status,
        key_prefix=row.key_prefix,
        key_type=row.key_type,
        activated_at=_aware(row.activated_at) if row.activated_at else None,
        expires_at=expires,
        days_remaining=days_remaining_for(row.expires_at, now=ref) if is_activated else None,
        usage_count=int(row.usage_count or 0),
        is_activated=is_activated,
    )


def inspect_access_key(
    session: Session,
    *,
    raw_key: str,
    device_id: str,
    user: Optional[UserRow] = None,
    email: Optional[str] = None,
    commit_usage: bool = False,
) -> AccessKeyStatus:
    key = raw_key.strip()
    device = device_id.strip()
    if not key:
        _reject(status.HTTP_401_UNAUTHORIZED, "access_key_required", "需要 Access Key。")
    if not device:
        _reject(status.HTTP_401_UNAUTHORIZED, "device_id_required", "需要设备 ID。")

    row = session.execute(
        select(AccessKeyRow).where(AccessKeyRow.key_hash == hash_access_key(key)).limit(1)
    ).scalar_one_or_none()
    if row is None:
        _reject(status.HTTP_401_UNAUTHORIZED, "invalid_access_key", "Access Key 无效。")
    if row.status != "active":
        _reject(status.HTTP_403_FORBIDDEN, "access_key_revoked", "Access Key 已停用。")

    now = _now()
    if row.activated_at is None:
        if commit_usage:
            row.activated_at = now
            row.expires_at = now + timedelta(days=max(1, int(row.duration_days or 1)))
    if row.expires_at is None and row.activated_at is not None:
        row.expires_at = _aware(row.activated_at) + timedelta(days=max(1, int(row.duration_days or 1)))

    if row.activated_at is not None and row.expires_at is not None and _aware(row.expires_at) <= now:
        _reject(status.HTTP_402_PAYMENT_REQUIRED, "access_key_expired", "Access Key 已过期。")

    if row.bound_device_id is None:
        if commit_usage:
            row.bound_device_id = device
    elif row.bound_device_id != device:
        _reject(status.HTTP_403_FORBIDDEN, "device_mismatch", "Access Key 已绑定其他设备。")

    if commit_usage:
        if user is not None and row.bound_user_id is None:
            row.bound_user_id = user.id
        bind_email = email or (user.email if user is not None else None)
        if bind_email and row.bound_email is None:
            row.bound_email = bind_email.strip().lower()
        row.usage_count = int(row.usage_count or 0) + 1
        row.last_used_at = now
        session.commit()
        session.refresh(row)

    return _status_from_row(row, now=now)


def validate_access_key(
    session: Session,
    *,
    raw_key: str,
    device_id: str,
    user: Optional[UserRow] = None,
    email: Optional[str] = None,
) -> AccessKeyGrant:
    status_row = inspect_access_key(
        session,
        raw_key=raw_key,
        device_id=device_id,
        user=user,
        email=email,
        commit_usage=True,
    )
    if status_row.expires_at is None:
        _reject(status.HTTP_500_INTERNAL_SERVER_ERROR, "access_key_state_error", "Access Key 状态异常。")
    return AccessKeyGrant(
        key_prefix=status_row.key_prefix,
        key_type=status_row.key_type,
        expires_at=status_row.expires_at,
        usage_count=status_row.usage_count,
    )


def get_access_key_by_prefix(session: Session, *, key_prefix: str) -> AccessKeyRow:
    row = session.execute(
        select(AccessKeyRow).where(AccessKeyRow.key_prefix == key_prefix.strip()).limit(1)
    ).scalar_one_or_none()
    if row is None:
        _reject(status.HTTP_404_NOT_FOUND, "access_key_not_found", "Access Key 不存在。")
    return row


def extend_access_key(session: Session, *, key_prefix: str, days: int) -> AccessKeyRow:
    row = get_access_key_by_prefix(session, key_prefix=key_prefix)
    now = _now()
    base = _aware(row.expires_at) if row.expires_at is not None and _aware(row.expires_at) > now else now
    row.expires_at = base + timedelta(days=max(1, int(days)))
    row.status = "active"
    session.commit()
    session.refresh(row)
    return row


def patch_access_key(
    session: Session,
    *,
    key_prefix: str,
    note: Optional[str] = None,
    duration_days: Optional[int] = None,
    expires_at: Optional[datetime] = None,
    max_devices: Optional[int] = None,
) -> AccessKeyRow:
    row = get_access_key_by_prefix(session, key_prefix=key_prefix)
    if note is not None:
        row.note = note.strip()
    if duration_days is not None:
        if row.activated_at is not None:
            _reject(
                status.HTTP_400_BAD_REQUEST,
                "duration_locked",
                "已激活的 Key 不能直接修改 duration_days，请使用延期或设置 expires_at。",
            )
        row.duration_days = max(1, int(duration_days))
    if expires_at is not None:
        aware_exp = _aware(expires_at)
        if aware_exp <= _now():
            _reject(status.HTTP_400_BAD_REQUEST, "invalid_expires_at", "到期时间必须晚于当前时间。")
        row.expires_at = aware_exp
        if row.activated_at is None:
            row.activated_at = _now()
    if max_devices is not None:
        row.max_devices = max(1, min(5, int(max_devices)))
    session.commit()
    session.refresh(row)
    return row


def unbind_access_key_device(session: Session, *, key_prefix: str) -> AccessKeyRow:
    row = get_access_key_by_prefix(session, key_prefix=key_prefix)
    row.bound_device_id = None
    session.commit()
    session.refresh(row)
    return row


def delete_access_key(session: Session, *, key_prefix: str) -> bool:
    row = get_access_key_by_prefix(session, key_prefix=key_prefix)
    if row.activated_at is not None:
        _reject(
            status.HTTP_400_BAD_REQUEST,
            "cannot_delete_activated",
            "已激活的 Key 不能删除，请使用撤销。",
        )
    session.delete(row)
    session.commit()
    return True
