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

LEADERBOARD_LIMIT = 300
LEADERBOARD_STAGGER_SECONDS = 1.5
UNUSUAL_VOL_OI_MIN = 3.0
UNUSUAL_VOLUME_MIN = 500
SELLER_MIN_VOLUME = 50
SELLER_MIN_STOCK_PRICE = 5.0
SELLER_MAX_SELL_ANN = 500.0
SELLER_MIN_DELTA = 0.15
SELLER_MAX_DELTA = 0.45

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
    sort_scope: str = "option"
    filters: tuple[BoardFilter, ...] = ()
    underlying_filters: tuple[BoardFilter, ...] = ()
    underlying_retrieves: tuple[str, ...] = ()
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
        # Futu does not support sort by IV_RANK (205); fetch by IV then re-rank client-side.
        sort_indicator="IMPLIED_VOLATILITY",
        sort_desc=True,
        underlying_retrieves=("IV_RANK",),
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
        filters=(
            # OTM only — deep ITM penny calls (e.g. HIVE $0.50C @ $4) inflate sell_ann.
            BoardFilter("IN_THE_MONEY", values=(0,)),
            BoardFilter("VOLUME", lower=SELLER_MIN_VOLUME),
        ),
        underlying_filters=(
            # Spot from Futu underlying.price (add_underlying_retrieve STOCK_PRICE).
            BoardFilter("STOCK_PRICE", lower=SELLER_MIN_STOCK_PRICE),
        ),
    ),
    "liquidity": BoardConfig(
        board_id="liquidity",
        sort_indicator="BID_ASK_SPREAD",
        sort_desc=False,
    ),
}


def _filter_dicts(filters: tuple[BoardFilter, ...]) -> list[dict[str, Any]]:
    return [
        {
            "indicator": f.indicator,
            "lower": f.lower,
            "upper": f.upper,
            "values": list(f.values) if f.values else None,
        }
        for f in filters
    ]


def _is_valid_seller_row(row: dict[str, Any]) -> bool:
    """Post-filter seller board: OTM sell candidates with sane annualized return."""
    sell_ann = row.get("sell_ann")
    if sell_ann is not None and float(sell_ann) > SELLER_MAX_SELL_ANN:
        return False

    spot_raw = row.get("underlying_price")
    spot = float(spot_raw) if isinstance(spot_raw, (int, float)) else None
    if spot is not None and spot < SELLER_MIN_STOCK_PRICE:
        return False

    strike_raw = row.get("strike")
    strike = float(strike_raw) if isinstance(strike_raw, (int, float)) else None
    contract_type = row.get("contract_type")

    if row.get("in_the_money") is True:
        return False

    if spot is not None and spot > 0 and strike is not None and strike > 0:
        if contract_type == "call" and strike < spot * 0.90:
            return False
        if contract_type == "put" and strike > spot * 1.10:
            return False

    delta_raw = row.get("delta")
    if isinstance(delta_raw, (int, float)):
        delta = abs(float(delta_raw))
        if delta < SELLER_MIN_DELTA or delta > SELLER_MAX_DELTA:
            return False

    return True


def _post_filter_board_items(board_id: BoardId, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if board_id == "seller":
        return [row for row in items if _is_valid_seller_row(row)]
    if board_id == "high-iv":
        ranked = sorted(
            items,
            key=lambda row: (
                float(row["iv_rank"]) if isinstance(row.get("iv_rank"), (int, float)) else -1.0,
                float(row["iv"]) if isinstance(row.get("iv"), (int, float)) else -1.0,
            ),
            reverse=True,
        )
        return ranked
    return items


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
        sort_scope=config.sort_scope,
        option_filters=_filter_dicts(config.filters),
        underlying_filters=_filter_dicts(config.underlying_filters),
        underlying_retrieves=config.underlying_retrieves,
        limit=config.limit,
    )
    if payload.get("error"):
        logger.warning("leaderboard refresh failed board=%s err=%s", board_id, payload.get("error"))
        return {**payload, "board": board_id}

    items = _post_filter_board_items(board_id, list(payload.get("items") or payload.get("contracts") or []))
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
    if not cached:
        cached = refresh_leaderboard_cache(board_id)
    elif not cached.get("items") and not cached.get("error"):
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


def get_options_sentiment(*, force_refresh: bool = False) -> dict[str, Any]:
    """Aggregate call/put volume sentiment from cached volume leaderboard."""
    cached = get_leaderboard("volume", force_refresh=force_refresh)
    items: list[dict[str, Any]] = list(cached.get("items") or [])

    calls = [row for row in items if row.get("option_type") == "C" or row.get("contract_type") == "call"]
    puts = [row for row in items if row.get("option_type") == "P" or row.get("contract_type") == "put"]

    call_volume = sum(int(row.get("volume") or 0) for row in calls)
    put_volume = sum(int(row.get("volume") or 0) for row in puts)
    pc_ratio = round(put_volume / call_volume, 4) if call_volume > 0 else None

    def _top_side(rows: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
        sorted_rows = sorted(rows, key=lambda r: int(r.get("volume") or 0), reverse=True)[:limit]
        out: list[dict[str, Any]] = []
        for index, row in enumerate(sorted_rows, start=1):
            out.append(
                {
                    "rank": index,
                    "underlying": row.get("underlying") or "",
                    "option_type": row.get("option_type") or "?",
                    "strike": row.get("strike"),
                    "expiry": row.get("expiry"),
                    "volume": int(row.get("volume") or 0),
                    "symbol_masked": row.get("symbol_masked"),
                }
            )
        return out

    return {
        "call_volume": call_volume,
        "put_volume": put_volume,
        "put_call_ratio": pc_ratio,
        "top_calls": _top_side(calls),
        "top_puts": _top_side(puts),
        "updated_at": cached.get("updated_at"),
        "cache_ttl_seconds": cached.get("cache_ttl_seconds", TTL_HOT),
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
    from app.services.membership import (
        MEMBER_LEADERBOARD_MAX_PAGES,
        MEMBER_LEADERBOARD_PAGE_SIZE,
        MEMBER_LEADERBOARD_ROW_LIMIT,
    )

    page = max(1, min(page, MEMBER_LEADERBOARD_MAX_PAGES))
    page_size = max(1, min(page_size, MEMBER_LEADERBOARD_PAGE_SIZE))
    cached = get_leaderboard("unusual", force_refresh=force_refresh)
    items = list(cached.get("items") or [])
    total = min(len(items), MEMBER_LEADERBOARD_ROW_LIMIT)
    start = (page - 1) * page_size
    end = start + page_size
    total_pages = max(1, min(MEMBER_LEADERBOARD_MAX_PAGES, (total + page_size - 1) // page_size))
    return {
        "contracts": items[start:end],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "universe_count": cached.get("universe_count"),
        "latency_ms": cached.get("latency_ms"),
        "synced_at": cached.get("updated_at"),
        "cache_ttl_seconds": cached.get("cache_ttl_seconds", TTL_HOT),
        "filters": {
            "vol_oi_min": UNUSUAL_VOL_OI_MIN,
            "volume_min": UNUSUAL_VOLUME_MIN,
            "limit": MEMBER_LEADERBOARD_ROW_LIMIT,
        },
        "error": cached.get("error"),
    }
