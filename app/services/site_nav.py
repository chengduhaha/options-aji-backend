"""Sidebar menu visibility — global settings for non-admin users."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import SiteNavSettingsRow
from app.services.cache_service import TTL_HOT, cache_delete, cache_get, cache_set

NAV_SETTINGS_KEY = "default"
CACHE_KEY_NAV = "site:nav_visibility"

# All menu ids that can be toggled (admin-only ids excluded from hiding)
KNOWN_NAV_IDS: tuple[str, ...] = (
    "aji_insights",
    "dash",
    "scanner",
    "stock",
    "feed",
    "ai",
    "learn",
    "macro",
    "settings",
    "profile",
    "divergence",
    "darkpool",
    "congress",
    "cross_market",
    "cross_scanner",
    "cross_feed",
    "ontology_copilot",
    "ontology_inspector",
)

NAV_GROUP_CHILDREN: dict[str, tuple[str, ...]] = {
    "alt_data": ("divergence", "darkpool", "congress"),
    "cross_market_group": (
        "cross_market",
        "cross_scanner",
        "cross_feed",
        "ontology_copilot",
        "ontology_inspector",
    ),
}


def default_visibility() -> dict[str, bool]:
    return {nav_id: True for nav_id in KNOWN_NAV_IDS}


def normalize_visibility(raw: Optional[dict[str, object]]) -> dict[str, bool]:
    base = default_visibility()
    if not raw:
        return base
    for nav_id in KNOWN_NAV_IDS:
        val = raw.get(nav_id)
        if isinstance(val, bool):
            base[nav_id] = val
    return base


def merge_visibility_update(
    current: dict[str, bool],
    patch: dict[str, bool],
) -> dict[str, bool]:
    out = dict(current)
    for nav_id, visible in patch.items():
        if nav_id in KNOWN_NAV_IDS:
            out[nav_id] = visible
        elif nav_id in NAV_GROUP_CHILDREN:
            for child in NAV_GROUP_CHILDREN[nav_id]:
                out[child] = visible
    return out


def load_visibility(session: Session) -> dict[str, bool]:
    cached = cache_get(CACHE_KEY_NAV)
    if isinstance(cached, dict):
        return normalize_visibility(cached)

    row = session.get(SiteNavSettingsRow, NAV_SETTINGS_KEY)
    vis = normalize_visibility(row.visibility if row else None)
    cache_set(CACHE_KEY_NAV, vis, ttl=60)
    return vis


def save_visibility(
    session: Session,
    visibility: dict[str, bool],
    *,
    updated_by_user_id: Optional[str] = None,
) -> dict[str, bool]:
    vis = normalize_visibility(visibility)
    row = session.get(SiteNavSettingsRow, NAV_SETTINGS_KEY)
    if row is None:
        row = SiteNavSettingsRow(key=NAV_SETTINGS_KEY, visibility=vis)
        session.add(row)
    else:
        row.visibility = vis
        if updated_by_user_id:
            row.updated_by_user_id = updated_by_user_id
    session.commit()
    cache_delete(CACHE_KEY_NAV)
    cache_set(CACHE_KEY_NAV, vis, ttl=60)
    return vis


def seed_nav_settings(session: Session) -> None:
    row = session.get(SiteNavSettingsRow, NAV_SETTINGS_KEY)
    if row is None:
        session.add(
            SiteNavSettingsRow(
                key=NAV_SETTINGS_KEY,
                visibility=default_visibility(),
            )
        )
        session.commit()
