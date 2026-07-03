"""V3 membership resolution — activation-code subscriptions on user accounts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, TYPE_CHECKING

from sqlalchemy import select

from app.db.models_user import ActivationCodeRow, UserRow

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

V3Tier = Literal["guest", "free", "member", "admin"]
MembershipKind = Literal["trial", "full"]

FREE_ROW_LIMIT = 10
FREE_SYMBOL_MASK_RANKS = 3
MEMBER_LEADERBOARD_ROW_LIMIT = 300
MEMBER_LEADERBOARD_PAGE_SIZE = 10
MEMBER_LEADERBOARD_MAX_PAGES = MEMBER_LEADERBOARD_ROW_LIMIT // MEMBER_LEADERBOARD_PAGE_SIZE
MEMBER_UNUSUAL_ROW_LIMIT = MEMBER_LEADERBOARD_ROW_LIMIT
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


def _provider_grants_full_access(session: Session, user: UserRow, *, now: datetime) -> bool:
    from app.services.entitlements import CommercialTier, resolve_commercial_entitlement

    ent = resolve_commercial_entitlement(session, user=user, now=now)
    return ent.tier == CommercialTier.PRO and ent.source in ("stripe", "creem")


def _infer_membership_kind(session: Session, user: UserRow) -> MembershipKind | None:
    stored = (user.membership_kind or "").strip().lower()
    if stored in ("trial", "full"):
        return stored  # type: ignore[return-value]

    row = session.scalars(
        select(ActivationCodeRow)
        .where(
            ActivationCodeRow.redeemed_by_user_id == user.id,
            ActivationCodeRow.status == "redeemed",
        )
        .order_by(ActivationCodeRow.redeemed_at.desc())
    ).first()
    if row is None:
        return "full"
    if row.duration_tier == "7D":
        return "trial"
    return "full"


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
    is_full_member: bool
    is_trial_member: bool
    membership_kind: MembershipKind | None
    membership_expires_at: datetime | None
    days_remaining: int | None


def resolve_v3_access(
    user: UserRow | None,
    *,
    session: Session | None = None,
    now: datetime | None = None,
) -> V3Access:
    current = now or datetime.now(timezone.utc)
    tier = resolve_v3_tier(user, now=current)
    active = membership_active(user, now=current)
    provider_full = bool(user and session and _provider_grants_full_access(session, user, now=current))

    membership_kind: MembershipKind | None = None
    if user is not None and tier == "admin":
        membership_kind = "full"
    elif user is not None and active and session is not None:
        membership_kind = _infer_membership_kind(session, user)
    elif user is not None and active:
        stored = (getattr(user, "membership_kind", None) or "").strip().lower()
        membership_kind = stored if stored in ("trial", "full") else "full"  # type: ignore[assignment]

    is_full_member = tier == "admin" or provider_full or (active and membership_kind == "full")
    is_trial_member = active and membership_kind == "trial" and not is_full_member
    is_member = tier in ("member", "admin") or provider_full

    expires = _as_aware(user.membership_expires_at) if user else None
    days_remaining: int | None = None
    if is_member and expires is not None and tier != "admin":
        delta = expires - current
        days_remaining = max(0, int(delta.total_seconds() // 86400))
    elif provider_full and user is not None and session is not None:
        from app.services.entitlements import CommercialTier, resolve_commercial_entitlement

        ent = resolve_commercial_entitlement(session, user=user, now=current)
        if ent.tier == CommercialTier.PRO and ent.current_period_end is not None:
            expires = _as_aware(ent.current_period_end)
            delta = ent.current_period_end - current
            days_remaining = max(0, int(delta.total_seconds() // 86400))

    return V3Access(
        tier=tier,
        is_member=is_member,
        is_full_member=is_full_member,
        is_trial_member=is_trial_member,
        membership_kind=membership_kind,
        membership_expires_at=expires,
        days_remaining=days_remaining,
    )


def membership_public_fields(access: V3Access) -> dict[str, object]:
    return {
        "tier": access.tier,
        "is_member": access.is_member,
        "is_full_member": access.is_full_member,
        "is_trial_member": access.is_trial_member,
        "membership_kind": access.membership_kind,
        "membership_expires_at": access.membership_expires_at.isoformat()
        if access.membership_expires_at
        else None,
        "days_remaining": access.days_remaining,
        "expiring_soon": access.days_remaining is not None and access.days_remaining <= 7,
    }


def blog_access_cache_tier(access: V3Access) -> str:
    if access.is_full_member:
        return "full"
    if access.is_trial_member:
        return "trial"
    return "guest"
