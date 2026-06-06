"""Access-key dependency for MVP entitlement checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Optional

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.api.deps_auth import extract_bearer_user_token
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.services.access_keys import AccessKeyGrant, validate_access_key
from app.services.entitlements import CommercialTier, resolve_commercial_entitlement
from app.services.jwt_tokens import decode_access_token

MvpTier = Literal["guest", "trial", "pro"]


@dataclass(frozen=True)
class MvpEntitlement:
    tier: MvpTier
    grant: AccessKeyGrant | None = None
    user: UserRow | None = None


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


def _admin_grant() -> AccessKeyGrant:
    now = datetime.now(timezone.utc)
    return AccessKeyGrant(
        key_prefix="admin_bypass",
        key_type="internal",
        expires_at=now + timedelta(days=3650),
        usage_count=0,
    )


async def resolve_mvp_entitlement(
    x_access_key: Annotated[Optional[str], Header(alias="X-Access-Key")] = None,
    x_device_id: Annotated[Optional[str], Header(alias="X-Device-Id")] = None,
    x_user_email: Annotated[Optional[str], Header(alias="X-User-Email")] = None,
    authorization: Annotated[Optional[str], Header(alias="Authorization")] = None,
    session: Session = Depends(db_session_dep),
) -> MvpEntitlement:
    user = _optional_user_from_authorization(authorization, session)
    entitlement = resolve_commercial_entitlement(
        session,
        user=user,
        raw_access_key=x_access_key or "",
        device_id=x_device_id or "",
        email=x_user_email,
        legacy_api_key=x_access_key or "",
    )
    if entitlement.tier in (CommercialTier.ADMIN, CommercialTier.PRO):
        return MvpEntitlement(
            tier="pro",
            grant=entitlement.grant or (_admin_grant() if entitlement.source == "admin" else None),
            user=user,
        )
    if entitlement.tier == CommercialTier.TRIAL:
        return MvpEntitlement(tier="trial", grant=None, user=user)
    return MvpEntitlement(tier="guest", grant=None, user=user)


async def require_access_key(
    x_access_key: Annotated[Optional[str], Header(alias="X-Access-Key")] = None,
    x_device_id: Annotated[Optional[str], Header(alias="X-Device-Id")] = None,
    x_user_email: Annotated[Optional[str], Header(alias="X-User-Email")] = None,
    authorization: Annotated[Optional[str], Header(alias="Authorization")] = None,
    session: Session = Depends(db_session_dep),
) -> AccessKeyGrant:
    user = _optional_user_from_authorization(authorization, session)
    if user is not None and user.role == "admin":
        return _admin_grant()
    return validate_access_key(
        session,
        raw_key=x_access_key or "",
        device_id=x_device_id or "",
        user=user,
        email=x_user_email,
    )
