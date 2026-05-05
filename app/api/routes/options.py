"""Options API routes — chain, snapshots, GEX, unusual activity."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, and_
from sqlalchemy.orm import Session

from app.api.deps import db_session_dep
from app.clients.massive_client import get_massive_client
from app.config import get_settings
from app.db.models import OptionsSnapshotRow
from app.services.cache_service import (
    TTL_HOT, cache_get, cache_set,
    key_options_chain, key_gex,
)
from app.analytics.gex_compute import compute_gex_profile

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/options", tags=["options"])


@router.get("/chain/{symbol}")
def get_options_chain(
    symbol: str,
    expiration_date: Optional[str] = Query(None),
    contract_type: Optional[str] = Query(None),
    strike_min: Optional[float] = Query(None),
    strike_max: Optional[float] = Query(None),
    limit: int = Query(500, le=1000),
    db: Session = Depends(db_session_dep),
):
    """Return options chain from DB (synced every 15 min) or live Massive API."""
    sym = symbol.upper()
    cfg = get_settings()

    # Try Redis cache first
    cache_key = key_options_chain(sym, expiration_date or "")
    cached = cache_get(cache_key)
    if cached:
        return cached

    # Try DB (populated by sync pipeline)
    query = select(OptionsSnapshotRow).where(OptionsSnapshotRow.underlying_ticker == sym)
    if expiration_date:
        query = query.where(OptionsSnapshotRow.expiration_date == expiration_date)
    if contract_type:
        query = query.where(OptionsSnapshotRow.contract_type == contract_type)
    if strike_min is not None:
        query = query.where(OptionsSnapshotRow.strike_price >= strike_min)
    if strike_max is not None:
        query = query.where(OptionsSnapshotRow.strike_price <= strike_max)
    query = query.order_by(OptionsSnapshotRow.expiration_date, OptionsSnapshotRow.strike_price).limit(limit)

    rows = db.execute(query).scalars().all()
    if rows:
        contracts = [
            {
                "ticker": r.ticker,
                "underlying": r.underlying_ticker,
                "contract_type": r.contract_type,
                "expiration_date": str(r.expiration_date) if r.expiration_date else None,
                "strike_price": r.strike_price,
                "delta": r.delta,
                "gamma": r.gamma,
                "theta": r.theta,
                "vega": r.vega,
                "implied_volatility": r.implied_volatility,
                "open_interest": r.open_interest,
                "bid": r.bid,
                "ask": r.ask,
                "bid_size": r.bid_size,
                "ask_size": r.ask_size,
                "midpoint": r.midpoint,
                "day_volume": r.day_volume,
                "day_change_pct": r.day_change_pct,
                "break_even_price": r.break_even_price,
                "underlying_price": r.underlying_price,
                "snapshot_time": r.snapshot_time.isoformat() if r.snapshot_time else None,
            }
            for r in rows
        ]
        # Get unique expirations
        expirations = sorted({str(r.expiration_date) for r in rows if r.expiration_date})
        result = {
            "symbol": sym,
            "source": "database",
            "expirations": expirations,
            "count": len(contracts),
            "contracts": contracts,
        }
        cache_set(cache_key, result, ttl=TTL_HOT)
        return result

    # Fallback: live Massive API
    if cfg.massive_api_key:
        try:
            client = get_massive_client()
            snapshots = client.get_option_chain_snapshot(
                sym,
                contract_type=contract_type,
                expiration_date=expiration_date,
                strike_price_gte=strike_min,
                strike_price_lte=strike_max,
            )
            result = {
                "symbol": sym,
                "source": "live_api",
                "count": len(snapshots),
                "contracts": snapshots,
            }
            cache_set(cache_key, result, ttl=TTL_HOT)
            return result
        except Exception as exc:
            logger.warning("Live options chain failed for %s: %s", sym, exc)

    return {"symbol": sym, "contracts": [], "error": "no_data"}


@router.get("/expirations/{symbol}")
def get_expirations(symbol: str, db: Session = Depends(db_session_dep)):
    """Return available expiration dates for a symbol."""
    sym = symbol.upper()
    from sqlalchemy import distinct
    exps = db.execute(
        select(distinct(OptionsSnapshotRow.expiration_date))
        .where(OptionsSnapshotRow.underlying_ticker == sym)
        .order_by(OptionsSnapshotRow.expiration_date)
    ).scalars().all()
    return {"symbol": sym, "expirations": [str(e) for e in exps if e]}


@router.get("/gex/{symbol}")
def get_gex(symbol: str):
    """Return Gamma Exposure profile (from cache, upstream, or local compute)."""
    sym = symbol.upper()
    cached = cache_get(key_gex(sym))
    if cached:
        return cached

    result = compute_gex_profile(sym)
    if not result.get("error"):
        cache_set(key_gex(sym), result, ttl=TTL_HOT)
    return result


@router.get("/unusual")
def get_unusual_options(
    vol_oi_min: float = Query(3.0),
    volume_min: int = Query(200),
    limit: int = Query(50, le=200),
    db: Session = Depends(db_session_dep),
):
    """Return unusual options activity (high volume/OI ratio)."""
    from sqlalchemy import case
    query = (
        select(OptionsSnapshotRow)
        .where(
            and_(
                OptionsSnapshotRow.day_volume >= volume_min,
                OptionsSnapshotRow.open_interest > 0,
            )
        )
        .order_by(
            (OptionsSnapshotRow.day_volume / OptionsSnapshotRow.open_interest).desc()
        )
        .limit(limit)
    )
    rows = db.execute(query).scalars().all()
    result = []
    for r in rows:
        ratio = (r.day_volume / r.open_interest) if r.open_interest else 0
        if ratio < vol_oi_min:
            continue
        result.append({
            "ticker": r.ticker,
            "underlying": r.underlying_ticker,
            "contract_type": r.contract_type,
            "expiration_date": str(r.expiration_date) if r.expiration_date else None,
            "strike_price": r.strike_price,
            "volume": r.day_volume,
            "open_interest": r.open_interest,
            "vol_oi_ratio": round(ratio, 2),
            "implied_volatility": r.implied_volatility,
            "delta": r.delta,
            "bid": r.bid,
            "ask": r.ask,
            "underlying_price": r.underlying_price,
        })
    return {"contracts": result, "count": len(result)}
