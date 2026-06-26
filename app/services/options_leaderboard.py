"""Cached Futu option-screen leaderboards (8 boards, 15-min refresh)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from app.clients.futu_client import get_futu_client
from app.services.cache_service import TTL_HOT, cache_get, cache_set, key_options_leaderboard

logger = logging.getLogger(__name__)

BoardId = Literal[
    "unusual",
    "volume",
    "open-interest",
    "turnover",
    "high-iv",
    "high-gamma",
    "seller",
    "liquidity",
]

LEADERBOARD_LIMIT = 150
LEADERBOARD_STAGGER_SECONDS = 1.5
UNUSUAL_VOL_OI_MIN = 3.0
UNUSUAL_VOLUME_MIN = 500

ALL_BOARD_IDS: tuple[BoardId, ...] = (
    "unusual",
    "volume",
    "open-interest",
    "turnover",
    "high-iv",
    "high-gamma",
    "seller",
    "liquidity",
)


@dataclass(frozen=True)
class BoardFilter:
    indicator: str
    lower: float | int | None = None
    upper: float | int | None = None
    values: tuple[int, ...] | None = None


@dataclass(frozen=True)
class BoardConfig:
    board_id: BoardId
    sort_indicator: str
    sort_desc: bool
    filters: tuple[BoardFilter, ...] = ()
    limit: int = LEADERBOARD_LIMIT


BOARD_CONFIGS: dict[BoardId, BoardConfig] = {
    "unusual": BoardConfig(
        board_id="unusual",
        sort_indicator="VOL_OI_RATIO",
        sort_desc=True,
        filters=(
            BoardFilter("VOL_OI_RATIO", lower=UNUSUAL_VOL_OI_MIN),
            BoardFilter("VOLUME", lower=UNUSUAL_VOLUME_MIN),
        ),
    ),
    "volume": BoardConfig(
        board_id="volume",
        sort_indicator="VOLUME",
        sort_desc=True,
    ),
    "open-interest": BoardConfig(
        board_id="open-interest",
        sort_indicator="OPEN_INTEREST",
        sort_desc=True,
    ),
    "turnover": BoardConfig(
        board_id="turnover",
        sort_indicator="TURNOVER",
        sort_desc=True,
    ),
    "high-iv": BoardConfig(
        board_id="high-iv",
        sort_indicator="IMPLIED_VOLATILITY",
        sort_desc=True,
    ),
    "high-gamma": BoardConfig(
        board_id="high-gamma",
        sort_indicator="GAMMA",
        sort_desc=True,
    ),
    "seller": BoardConfig(
        board_id="seller",
        sort_indicator="SELL_ANNUALIZED_RETURN",
        sort_desc=True,
    ),
    "liquidity": BoardConfig(
        board_id="liquidity",
        sort_indicator="BID_ASK_SPREAD",
        sort_desc=False,
    ),
}


def _normalize_board_id(board: str) -> BoardId | None:
    key = board.strip().lower()
    if key in BOARD_CONFIGS:
        return key  # type: ignore[return-value]
    return None


def refresh_leaderboard_cache(board: str) -> dict[str, Any]:
    """Pull one board from Futu and store in Redis (TTL 15 min)."""
    board_id = _normalize_board_id(board)
    if board_id is None:
        return {"error": "unknown_board", "board": board, "items": [], "total": 0}
    config = BOARD_CONFIGS[board_id]
    futu = get_futu_client()
    payload = futu.get_option_screen_board(
        sort_indicator=config.sort_indicator,
        sort_desc=config.sort_desc,
        option_filters=[
            {
                "indicator": f.indicator,
                "lower": f.lower,
                "upper": f.upper,
                "values": list(f.values) if f.values else None,
            }
            for f in config.filters
        ],
        limit=config.limit,
    )
    if payload.get("error"):
        logger.warning("leaderboard refresh failed board=%s err=%s", board_id, payload.get("error"))
        return {**payload, "board": board_id}

    items = payload.get("items") or payload.get("contracts") or []
    for index, row in enumerate(items, start=1):
        row["rank"] = index

    stored = {
        "board": board_id,
        "items": items,
        "total": len(items),
        "universe_count": payload.get("universe_count"),
        "latency_ms": payload.get("latency_ms"),
        "synced_at": payload.get("synced_at") or datetime.now(timezone.utc).isoformat(),
    }
    cache_set(key_options_leaderboard(board_id), stored, ttl=TTL_HOT)
    logger.info(
        "leaderboard refreshed board=%s items=%s universe=%s",
        board_id,
        stored["total"],
        stored.get("universe_count"),
    )
    return stored


def refresh_all_leaderboards_cache() -> dict[str, Any]:
    """Refresh all boards with staggered Futu calls."""
    results: dict[str, Any] = {}
    for index, board_id in enumerate(ALL_BOARD_IDS):
        results[board_id] = refresh_leaderboard_cache(board_id)
        if index < len(ALL_BOARD_IDS) - 1:
            time.sleep(LEADERBOARD_STAGGER_SECONDS)
    return results


def get_leaderboard(
    board: str,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Return full cached leaderboard for client-side filtering."""
    board_id = _normalize_board_id(board)
    if board_id is None:
        return {"error": "unknown_board", "board": board, "items": [], "total": 0}

    cached = None if force_refresh else cache_get(key_options_leaderboard(board_id))
    if not cached or not cached.get("items"):
        cached = refresh_leaderboard_cache(board_id)

    items: list[dict[str, Any]] = list(cached.get("items") or [])
    return {
        "board": board_id,
        "items": items,
        "total": len(items),
        "universe_count": cached.get("universe_count"),
        "latency_ms": cached.get("latency_ms"),
        "updated_at": cached.get("synced_at"),
        "cache_ttl_seconds": TTL_HOT,
        "error": cached.get("error"),
    }


# Backward compatibility for unusual-leaderboard endpoint
def refresh_unusual_leaderboard_cache(
    *,
    vol_oi_min: float = UNUSUAL_VOL_OI_MIN,
    volume_min: int = UNUSUAL_VOLUME_MIN,
) -> dict[str, Any]:
    del vol_oi_min, volume_min
    return refresh_leaderboard_cache("unusual")


def get_unusual_leaderboard_page(
    *,
    page: int = 1,
    page_size: int = 10,
    vol_oi_min: float = UNUSUAL_VOL_OI_MIN,
    volume_min: int = UNUSUAL_VOLUME_MIN,
    force_refresh: bool = False,
) -> dict[str, Any]:
    del vol_oi_min, volume_min
    page = max(1, min(page, 10))
    page_size = max(1, min(page_size, 10))
    cached = get_leaderboard("unusual", force_refresh=force_refresh)
    items = list(cached.get("items") or [])
    total = min(len(items), 100)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "contracts": items[start:end],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": 10,
        "universe_count": cached.get("universe_count"),
        "latency_ms": cached.get("latency_ms"),
        "synced_at": cached.get("updated_at"),
        "cache_ttl_seconds": cached.get("cache_ttl_seconds", TTL_HOT),
        "filters": {"vol_oi_min": UNUSUAL_VOL_OI_MIN, "volume_min": UNUSUAL_VOLUME_MIN, "limit": 100},
        "error": cached.get("error"),
    }
