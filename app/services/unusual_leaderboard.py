"""Cached Futu unusual-contracts leaderboard (top 100, 15-min refresh)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.clients.futu_client import get_futu_client
from app.services.cache_service import TTL_HOT, cache_get, cache_set, key_unusual_leaderboard

logger = logging.getLogger(__name__)

LEADERBOARD_TOTAL = 100
LEADERBOARD_PAGE_SIZE = 10
LEADERBOARD_PAGES = 10
DEFAULT_VOL_OI_MIN = 3.0
DEFAULT_VOLUME_MIN = 500


def refresh_unusual_leaderboard_cache(
    *,
    vol_oi_min: float = DEFAULT_VOL_OI_MIN,
    volume_min: int = DEFAULT_VOLUME_MIN,
) -> dict[str, Any]:
    """Pull top contracts from Futu and store in Redis (TTL 15 min)."""
    futu = get_futu_client()
    payload = futu.get_option_screen_leaderboard(
        vol_oi_min=vol_oi_min,
        volume_min=volume_min,
        limit=LEADERBOARD_TOTAL,
    )
    if payload.get("error"):
        logger.warning("unusual leaderboard refresh failed: %s", payload.get("error"))
        return payload

    contracts = payload.get("contracts") or []
    for index, row in enumerate(contracts, start=1):
        row["rank"] = index

    stored = {
        "contracts": contracts,
        "total": len(contracts),
        "universe_count": payload.get("universe_count"),
        "latency_ms": payload.get("latency_ms"),
        "source": payload.get("source", "futu"),
        "filters": payload.get("filters")
        or {"vol_oi_min": vol_oi_min, "volume_min": volume_min, "limit": LEADERBOARD_TOTAL},
        "synced_at": payload.get("synced_at") or datetime.now(timezone.utc).isoformat(),
    }
    cache_set(key_unusual_leaderboard(), stored, ttl=TTL_HOT)
    logger.info(
        "unusual leaderboard refreshed: %s contracts, universe=%s",
        stored["total"],
        stored.get("universe_count"),
    )
    return stored


def get_unusual_leaderboard_page(
    *,
    page: int = 1,
    page_size: int = LEADERBOARD_PAGE_SIZE,
    vol_oi_min: float = DEFAULT_VOL_OI_MIN,
    volume_min: int = DEFAULT_VOLUME_MIN,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Return paginated slice from cached leaderboard; refresh on miss or force."""
    page = max(1, min(page, LEADERBOARD_PAGES))
    page_size = max(1, min(page_size, LEADERBOARD_PAGE_SIZE))

    cached = None if force_refresh else cache_get(key_unusual_leaderboard())
    if not cached or not cached.get("contracts"):
        cached = refresh_unusual_leaderboard_cache(vol_oi_min=vol_oi_min, volume_min=volume_min)

    contracts: list[dict[str, Any]] = list(cached.get("contracts") or [])
    total = min(len(contracts), LEADERBOARD_TOTAL)
    start = (page - 1) * page_size
    end = start + page_size
    page_rows = contracts[start:end]

    return {
        "contracts": page_rows,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": LEADERBOARD_PAGES,
        "universe_count": cached.get("universe_count"),
        "latency_ms": cached.get("latency_ms"),
        "source": cached.get("source", "futu"),
        "filters": cached.get("filters")
        or {"vol_oi_min": vol_oi_min, "volume_min": volume_min, "limit": LEADERBOARD_TOTAL},
        "synced_at": cached.get("synced_at"),
        "cache_ttl_seconds": TTL_HOT,
        "error": cached.get("error"),
    }
