"""Unified commercial entitlement resolution for public MVP and paid features."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import ApiEntitlementRow
from app.db.models_user import UserRow
from app.services.access_keys import AccessKeyGrant, inspect_access_key
from app.services.membership import membership_active

EntitlementSource = Literal[
    "anonymous",
    "jwt",
    "creem",
    "stripe",
    "access_key",
    "admin",
    "legacy_api_key",
]


class CommercialTier(StrEnum):
    GUEST = "guest"
    TRIAL = "trial"
    FREE = "free"
    PRO = "pro"
    ADMIN = "admin"


@dataclass(frozen=True)
class CommercialEntitlement:
    tier: CommercialTier
    source: EntitlementSource
    user: UserRow | None = None
    row: ApiEntitlementRow | None = None
    grant: AccessKeyGrant | None = None
    provider_status: str | None = None
    current_period_end: datetime | None = None
    in_grace_period: bool = False


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _period_active(row: ApiEntitlementRow, now: datetime) -> bool:
    end = _as_aware(row.current_period_end)
    return end is None or end > now


def _past_due_in_grace(row: ApiEntitlementRow, now: datetime, *, grace_days: int = 7) -> bool:
    since = _as_aware(row.past_due_since)
    if since is None:
        return True
    return since + timedelta(days=grace_days) > now


def _provider_source(row: ApiEntitlementRow) -> EntitlementSource:
    provider = (row.provider or "").strip().lower()
    if provider in ("creem", "stripe"):
        return provider  # type: ignore[return-value]
    if (row.stripe_customer_id or "").strip():
        return "stripe"
    return "legacy_api_key"


def _row_is_provider_pro(row: ApiEntitlementRow, now: datetime) -> tuple[bool, bool]:
    status = (row.provider_status or "").strip().lower()
    if status in ("active", "trialing"):
        return _period_active(row, now), False
    if status == "past_due":
        in_grace = _past_due_in_grace(row, now)
        return in_grace, in_grace
    if not status and row.plan == "pro":
        return _period_active(row, now), False
    return False, False


def _candidate_rows(
    session: Session,
    *,
    user: UserRow | None,
    legacy_api_key: str,
) -> list[ApiEntitlementRow]:
    clauses = []
    if user is not None:
        clauses.append(ApiEntitlementRow.user_id == user.id)
    if legacy_api_key.strip():
        clauses.append(ApiEntitlementRow.api_key == legacy_api_key.strip())
    if not clauses:
        return []
    return list(session.scalars(select(ApiEntitlementRow).where(or_(*clauses))).all())


def _try_access_key(
    session: Session,
    *,
    raw_access_key: str,
    device_id: str,
    user: UserRow | None,
    email: str | None,
) -> AccessKeyGrant | None:
    if not raw_access_key.strip() or not device_id.strip():
        return None
    try:
        status = inspect_access_key(
            session,
            raw_key=raw_access_key.strip(),
            device_id=device_id.strip(),
            user=user,
            email=email,
            commit_usage=True,
        )
    except HTTPException:
        return None
    if not status.valid or status.expires_at is None:
        return None
    return AccessKeyGrant(
        key_prefix=status.key_prefix,
        key_type=status.key_type,
        expires_at=status.expires_at,
        usage_count=status.usage_count,
    )


def resolve_commercial_entitlement(
    session: Session,
    *,
    user: UserRow | None = None,
    raw_access_key: str = "",
    device_id: str = "",
    email: str | None = None,
    legacy_api_key: str = "",
    trial_enabled: bool | None = None,
    now: datetime | None = None,
) -> CommercialEntitlement:
    """Resolve the highest-priority entitlement for a request.

    Priority: admin JWT, active/trialing/past_due provider subscription, Access Key,
    verified-user trial, signed-in free user, anonymous guest.
    """

    current = now or datetime.now(timezone.utc)
    if user is not None and user.role == "admin":
        return CommercialEntitlement(tier=CommercialTier.ADMIN, source="admin", user=user)

    if user is not None and membership_active(user, now=current):
        return CommercialEntitlement(
            tier=CommercialTier.PRO,
            source="jwt",
            user=user,
            current_period_end=user.membership_expires_at,
        )

    rows = _candidate_rows(session, user=user, legacy_api_key=legacy_api_key)
    for row in rows:
        is_pro, in_grace = _row_is_provider_pro(row, current)
        if is_pro:
            source = _provider_source(row)
            return CommercialEntitlement(
                tier=CommercialTier.PRO,
                source=source,
                user=user,
                row=row,
                provider_status=row.provider_status,
                current_period_end=row.current_period_end,
                in_grace_period=in_grace,
            )

    grant = _try_access_key(
        session,
        raw_access_key=raw_access_key,
        device_id=device_id,
        user=user,
        email=email,
    )
    if grant is not None:
        return CommercialEntitlement(
            tier=CommercialTier.PRO,
            source="access_key",
            user=user,
            grant=grant,
        )

    if user is not None and user.role != "disabled":
        enabled = get_settings().mvp_trial_enabled if trial_enabled is None else trial_enabled
        if bool(user.email_verified) and enabled:
            return CommercialEntitlement(tier=CommercialTier.TRIAL, source="jwt", user=user)
        return CommercialEntitlement(tier=CommercialTier.FREE, source="jwt", user=user)

    return CommercialEntitlement(tier=CommercialTier.GUEST, source="anonymous")
