"""Dark Pool & Options Flow Radar endpoints."""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.api.deps import bearer_subscription_optional
from app.db.models import MarketTideDailyRow, OptionsSnapshotRow
from app.db.session import db_session_dep
from app.services.cache_service import TTL_HOT, cache_get, cache_set

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/darkpool", tags=["dark_pool"])

_TTL = 300  # 5 min


def _compute_tide(db: Session) -> dict:
    """Compute call vs put premium flow from latest options snapshots."""
    try:
        rows = db.execute(
            select(
                OptionsSnapshotRow.contract_type,
                func.sum(OptionsSnapshotRow.midpoint * OptionsSnapshotRow.day_volume).label("net_premium"),
                func.sum(OptionsSnapshotRow.day_volume).label("total_volume"),
            )
            .where(
                and_(
                    OptionsSnapshotRow.day_volume > 0,
                    OptionsSnapshotRow.midpoint > 0,
                )
            )
            .group_by(OptionsSnapshotRow.contract_type)
        ).all()

        call_premium = 0.0
        put_premium = 0.0
        call_volume = 0
        put_volume = 0
        for r in rows:
            if r.contract_type == "call":
                call_premium = float(r.net_premium or 0)
                call_volume = int(r.total_volume or 0)
            elif r.contract_type == "put":
                put_premium = float(r.net_premium or 0)
                put_volume = int(r.total_volume or 0)

        net_flow = call_premium - put_premium
        pc_ratio = put_volume / max(call_volume, 1)
        tide = "bullish" if net_flow > 0 else "bearish" if net_flow < 0 else "neutral"
        return {
            "call_premium_total": int(call_premium),
            "put_premium_total": int(put_premium),
            "net_call_flow": int(net_flow),
            "call_volume": call_volume,
            "put_volume": put_volume,
            "put_call_ratio": round(pc_ratio, 4),
            "tide_direction": tide,
        }
    except Exception as exc:
        logger.warning("compute_tide failed: %s", exc)
        return {}


@router.get("/market-tide")
def get_market_tide(
    days: int = Query(7, ge=1, le=30),
    db: Session = Depends(db_session_dep),
    _: Optional[str] = Depends(bearer_subscription_optional),
):
    """Return today's options market tide and historical daily tide data."""
    cache_key = f"darkpool:tide:{days}"
    if hit := cache_get(cache_key):
        return hit

    today_tide = _compute_tide(db)

    history_rows = db.execute(
        select(MarketTideDailyRow)
        .order_by(MarketTideDailyRow.trade_date.desc())
        .limit(days)
    ).scalars().all()

    history = [
        {
            "date": str(r.trade_date),
            "call_premium": r.call_premium_total,
            "put_premium": r.put_premium_total,
            "net_flow": r.net_call_flow,
            "put_call_ratio": r.put_call_ratio,
            "direction": r.tide_direction,
        }
        for r in reversed(history_rows)
    ]

    result = {
        "today": {"date": date.today().isoformat(), **today_tide},
        "history": history,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set(cache_key, result, ttl=_TTL)
    return result


@router.get("/flow-summary")
def get_flow_summary(
    top_n: int = Query(20, ge=5, le=50),
    min_premium_usd: float = Query(100_000, ge=0),
    db: Session = Depends(db_session_dep),
    _: Optional[str] = Depends(bearer_subscription_optional),
):
    """Return top symbols by estimated options premium flow (call vs put)."""
    cache_key = f"darkpool:flow:{top_n}:{int(min_premium_usd)}"
    if hit := cache_get(cache_key):
        return hit

    rows = db.execute(
        select(
            OptionsSnapshotRow.underlying_ticker,
            OptionsSnapshotRow.contract_type,
            func.sum(
                OptionsSnapshotRow.midpoint * OptionsSnapshotRow.day_volume * 100
            ).label("est_premium"),
            func.sum(OptionsSnapshotRow.day_volume).label("total_volume"),
            func.count().label("contract_count"),
        )
        .where(
            and_(
                OptionsSnapshotRow.day_volume > 0,
                OptionsSnapshotRow.midpoint > 0,
            )
        )
        .group_by(
            OptionsSnapshotRow.underlying_ticker,
            OptionsSnapshotRow.contract_type,
        )
    ).all()

    symbol_map: dict[str, dict] = {}
    for r in rows:
        sym = r.underlying_ticker
        est = float(r.est_premium or 0)
        if est < min_premium_usd:
            continue
        if sym not in symbol_map:
            symbol_map[sym] = {"symbol": sym, "call_premium": 0, "put_premium": 0,
                               "call_volume": 0, "put_volume": 0}
        if r.contract_type == "call":
            symbol_map[sym]["call_premium"] += int(est)
            symbol_map[sym]["call_volume"] += int(r.total_volume or 0)
        elif r.contract_type == "put":
            symbol_map[sym]["put_premium"] += int(est)
            symbol_map[sym]["put_volume"] += int(r.total_volume or 0)

    flow_items = []
    for sd in symbol_map.values():
        net = sd["call_premium"] - sd["put_premium"]
        total = sd["call_premium"] + sd["put_premium"]
        flow_items.append({
            **sd,
            "net_flow_usd": net,
            "direction": "bullish" if net > 0 else "bearish",
            "total_premium_usd": total,
        })

    flow_items.sort(key=lambda x: x["total_premium_usd"], reverse=True)

    result = {
        "items": flow_items[:top_n],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set(cache_key, result, ttl=_TTL)
    return result


@router.get("/bubble-data/{symbol}")
def get_bubble_data(
    symbol: str,
    db: Session = Depends(db_session_dep),
    _: Optional[str] = Depends(bearer_subscription_optional),
):
    """Return bubble chart data for a symbol (strike × expiry × premium size)."""
    sym = symbol.upper()
    cache_key = f"darkpool:bubble:{sym}"
    if hit := cache_get(cache_key):
        return hit

    rows = db.execute(
        select(OptionsSnapshotRow)
        .where(
            and_(
                OptionsSnapshotRow.underlying_ticker == sym,
                OptionsSnapshotRow.day_volume > 0,
                OptionsSnapshotRow.midpoint > 0,
            )
        )
        .order_by(
            (OptionsSnapshotRow.day_volume * OptionsSnapshotRow.midpoint).desc()
        )
        .limit(300)
    ).scalars().all()

    bubbles = []
    for r in rows:
        est_premium = float(r.midpoint or 0) * float(r.day_volume or 0) * 100
        if est_premium < 10_000:
            continue
        oi = r.open_interest or 0
        vol = r.day_volume or 0
        vol_oi = round(vol / max(oi, 1), 2)
        if r.contract_type == "call":
            sentiment = "strong_bullish" if vol_oi > 5 else "bullish"
        else:
            sentiment = "strong_bearish" if vol_oi > 5 else "bearish"
        bubbles.append({
            "ticker": r.ticker,
            "strike": r.strike_price,
            "expiration": str(r.expiration_date) if r.expiration_date else None,
            "contract_type": r.contract_type,
            "volume": vol,
            "open_interest": oi,
            "vol_oi_ratio": vol_oi,
            "midpoint": r.midpoint,
            "est_premium_usd": int(est_premium),
            "implied_volatility": r.implied_volatility,
            "delta": r.delta,
            "sentiment": sentiment,
        })

    result = {
        "symbol": sym,
        "bubbles": bubbles,
        "total": len(bubbles),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set(cache_key, result, ttl=TTL_HOT)
    return result
