"""V3 membership resolution — activation-code subscriptions on user accounts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from app.db.models_user import UserRow

V3Tier = Literal["guest", "free", "member", "admin"]

FREE_ROW_LIMIT = 10
FREE_SYMBOL_MASK_RANKS = 3
MEMBER_UNUSUAL_ROW_LIMIT = 100
FREE_GEX_SYMBOL = "SPY"


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def membership_active(user: UserRow | None, *, now: datetime | None = None) -> bool:
    if user is None:
        return False
    if user.role == "admin":
        return True
    expires = _as_aware(user.membership_expires_at)
    if expires is None:
        return False
    current = now or datetime.now(timezone.utc)
    return expires > current


def resolve_v3_tier(user: UserRow | None, *, now: datetime | None = None) -> V3Tier:
    if user is None:
        return "guest"
    if user.role == "admin":
        return "admin"
    if membership_active(user, now=now):
        return "member"
    return "free"


@dataclass(frozen=True)
class V3Access:
    tier: V3Tier
    is_member: bool
    membership_expires_at: datetime | None
    days_remaining: int | None


def resolve_v3_access(user: UserRow | None, *, now: datetime | None = None) -> V3Access:
    current = now or datetime.now(timezone.utc)
    tier = resolve_v3_tier(user, now=current)
    is_member = tier in ("member", "admin")
    expires = _as_aware(user.membership_expires_at) if user else None
    days_remaining: int | None = None
    if is_member and expires is not None and tier != "admin":
        delta = expires - current
        days_remaining = max(0, int(delta.total_seconds() // 86400))
    return V3Access(
        tier=tier,
        is_member=is_member,
        membership_expires_at=expires,
        days_remaining=days_remaining,
    )


def membership_public_fields(access: V3Access) -> dict[str, object]:
    return {
        "tier": access.tier,
        "is_member": access.is_member,
        "membership_expires_at": access.membership_expires_at.isoformat()
        if access.membership_expires_at
        else None,
        "days_remaining": access.days_remaining,
        "expiring_soon": access.days_remaining is not None and access.days_remaining <= 7,
    }
