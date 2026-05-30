"""Admin routes for manual trial/paid access keys."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps_auth import extract_bearer_user_token, get_current_admin_user
from app.db.models import AccessKeyRow
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.services.access_keys import (
    create_access_key,
    days_remaining_for,
    delete_access_key,
    extend_access_key,
    get_access_key_by_prefix,
    inspect_access_key,
    patch_access_key,
    unbind_access_key_device,
)
from app.services.jwt_tokens import decode_access_token

router = APIRouter(prefix="/api/access-keys", tags=["access-keys"])


class AccessKeyCreateBody(BaseModel):
    key_type: str = Field(default="trial", max_length=16)
    duration_days: int = Field(default=7, ge=1, le=366)
    note: str = Field(default="", max_length=500)
    max_devices: int = Field(default=1, ge=1, le=5)


class AccessKeyExtendBody(BaseModel):
    days: int = Field(default=30, ge=1, le=366)


class AccessKeyPatchBody(BaseModel):
    note: Optional[str] = Field(default=None, max_length=500)
    duration_days: Optional[int] = Field(default=None, ge=1, le=366)
    expires_at: Optional[datetime] = None
    max_devices: Optional[int] = Field(default=None, ge=1, le=5)


def _optional_user_from_authorization(
    authorization: Optional[str],
    session: Session,
) -> Optional[UserRow]:
    token = extract_bearer_user_token(authorization)
    if not token:
        return None
    payload = decode_access_token(token)
    if not isinstance(payload, dict):
        return None
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        return None
    return session.get(UserRow, sub)


def _row_out(row: AccessKeyRow) -> dict[str, object]:
    is_activated = row.activated_at is not None
    return {
        "key_prefix": row.key_prefix,
        "key_type": row.key_type,
        "status": row.status,
        "duration_days": row.duration_days,
        "expires_at": row.expires_at.astimezone(timezone.utc).isoformat() if row.expires_at else None,
        "activated_at": row.activated_at.astimezone(timezone.utc).isoformat() if row.activated_at else None,
        "is_activated": is_activated,
        "days_remaining": days_remaining_for(row.expires_at) if is_activated else None,
        "bound_email": row.bound_email,
        "bound_device_id": row.bound_device_id,
        "bound_user_id": row.bound_user_id,
        "max_devices": row.max_devices,
        "usage_count": row.usage_count,
        "last_used_at": row.last_used_at.astimezone(timezone.utc).isoformat() if row.last_used_at else None,
        "note": row.note,
        "created_at": row.created_at.astimezone(timezone.utc).isoformat() if row.created_at else None,
    }


def _status_out(status_row) -> dict[str, object]:
    return {
        "valid": status_row.valid,
        "status": status_row.status,
        "key_type": status_row.key_type,
        "key_prefix": status_row.key_prefix,
        "activated_at": status_row.activated_at.isoformat() if status_row.activated_at else None,
        "expires_at": status_row.expires_at.isoformat() if status_row.expires_at else None,
        "days_remaining": status_row.days_remaining,
        "usage_count": status_row.usage_count,
        "is_activated": status_row.is_activated,
    }


@router.post("/status")
def access_key_status(
    x_access_key: Annotated[Optional[str], Header(alias="X-Access-Key")] = None,
    x_device_id: Annotated[Optional[str], Header(alias="X-Device-Id")] = None,
    x_user_email: Annotated[Optional[str], Header(alias="X-User-Email")] = None,
    authorization: Annotated[Optional[str], Header(alias="Authorization")] = None,
    session: Session = Depends(db_session_dep),
) -> dict[str, object]:
    user = _optional_user_from_authorization(authorization, session)
    status_row = inspect_access_key(
        session,
        raw_key=x_access_key or "",
        device_id=x_device_id or "",
        user=user,
        email=x_user_email,
        commit_usage=False,
    )
    return {"success": True, "data": _status_out(status_row)}


@router.post("")
def create_key(
    body: AccessKeyCreateBody,
    session: Session = Depends(db_session_dep),
    _: UserRow = Depends(get_current_admin_user),
) -> dict[str, object]:
    issued = create_access_key(
        session,
        key_type=body.key_type,
        duration_days=body.duration_days,
        note=body.note,
        max_devices=body.max_devices,
    )
    row = session.execute(
        select(AccessKeyRow).where(AccessKeyRow.key_prefix == issued.key_prefix).limit(1)
    ).scalar_one()
    return {"success": True, "raw_key": issued.raw_key, "data": _row_out(row)}


@router.get("")
def list_keys(
    limit: int = Query(default=50, ge=1, le=200),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    key_type: Optional[str] = Query(default=None),
    bound_email: Optional[str] = Query(default=None),
    session: Session = Depends(db_session_dep),
    _: UserRow = Depends(get_current_admin_user),
) -> dict[str, object]:
    stmt = select(AccessKeyRow).order_by(AccessKeyRow.created_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(AccessKeyRow.status == status_filter.strip().lower())
    if key_type:
        stmt = stmt.where(AccessKeyRow.key_type == key_type.strip().lower())
    if bound_email:
        stmt = stmt.where(AccessKeyRow.bound_email == bound_email.strip().lower())
    rows = session.execute(stmt).scalars().all()
    return {"success": True, "data": [_row_out(row) for row in rows]}


@router.get("/{key_prefix}")
def get_key(
    key_prefix: str,
    session: Session = Depends(db_session_dep),
    _: UserRow = Depends(get_current_admin_user),
) -> dict[str, object]:
    row = get_access_key_by_prefix(session, key_prefix=key_prefix)
    return {"success": True, "data": _row_out(row)}


@router.patch("/{key_prefix}")
def patch_key(
    key_prefix: str,
    body: AccessKeyPatchBody,
    session: Session = Depends(db_session_dep),
    _: UserRow = Depends(get_current_admin_user),
) -> dict[str, object]:
    row = patch_access_key(
        session,
        key_prefix=key_prefix,
        note=body.note,
        duration_days=body.duration_days,
        expires_at=body.expires_at,
        max_devices=body.max_devices,
    )
    return {"success": True, "data": _row_out(row)}


@router.delete("/{key_prefix}")
def delete_key(
    key_prefix: str,
    session: Session = Depends(db_session_dep),
    _: UserRow = Depends(get_current_admin_user),
) -> dict[str, object]:
    delete_access_key(session, key_prefix=key_prefix)
    return {"success": True, "deleted": 1}


@router.post("/{key_prefix}/extend")
def extend_key(
    key_prefix: str,
    body: AccessKeyExtendBody,
    session: Session = Depends(db_session_dep),
    _: UserRow = Depends(get_current_admin_user),
) -> dict[str, object]:
    row = extend_access_key(session, key_prefix=key_prefix, days=body.days)
    return {"success": True, "data": _row_out(row)}


@router.post("/{key_prefix}/revoke")
def revoke_key(
    key_prefix: str,
    session: Session = Depends(db_session_dep),
    _: UserRow = Depends(get_current_admin_user),
) -> dict[str, object]:
    row: Optional[AccessKeyRow] = session.execute(
        select(AccessKeyRow).where(AccessKeyRow.key_prefix == key_prefix.strip()).limit(1)
    ).scalar_one_or_none()
    if row is None:
        return {"success": True, "revoked": 0}
    row.status = "revoked"
    session.commit()
    return {"success": True, "revoked": 1}


@router.post("/{key_prefix}/unbind-device")
def unbind_device(
    key_prefix: str,
    session: Session = Depends(db_session_dep),
    _: UserRow = Depends(get_current_admin_user),
) -> dict[str, object]:
    row = unbind_access_key_device(session, key_prefix=key_prefix)
    return {"success": True, "data": _row_out(row)}
