"""Options API routes — chain, snapshots, GEX, unusual activity."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, and_
from sqlalchemy.orm import Session

from app.db.session import db_session_dep
from app.clients.massive_client import get_massive_client
from app.config import get_settings
from app.db.models import OptionsSnapshotRow
from app.services.cache_service import (
    TTL_HOT, cache_get, cache_set,
    key_options_chain, key_gex,
)
from app.analytics.gex_compute import compute_gex_profile
from app.analytics.gex_history import record_gex_snapshot
from app.analytics.unusual_v2 import score_snapshot_rows

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
                limit=min(limit, 250),
                max_contracts=limit,
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
        record_gex_snapshot(sym, dict(result))
    return result


@router.get("/gex/history/{symbol}")
def get_gex_history_endpoint(
    symbol: str,
    days: int = Query(120, ge=10, le=400),
):
    """Sparse GEX points from Redis snapshots + Yahoo daily closes."""

    sym = symbol.upper()
    from app.analytics.gex_history import list_gex_history, seed_price_closes

    return {
        "symbol": sym,
        "gexSeries": list_gex_history(sym, limit_days=days),
        "priceCloses": seed_price_closes(sym, days=max(days, 60)),
    }


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


@router.get("/unusual-v2")
def unusual_options_v2_global(
    symbol: Optional[str] = Query(None, description="Filter by underlying; omit for all"),
    min_score: int = Query(60, ge=0, le=100),
    sort_by: str = Query("score"),
    order: str = Query("desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    volume_min: int = Query(40, ge=0),
    db: Session = Depends(db_session_dep),
):
    """Unusual scans `options_snapshots` in Postgres — synced from Massive (not FMP)."""

    if sort_by not in ("score", "estimated_flow", "volume", "strike"):
        sort_by = "score"
    descending = order.lower() != "asc"
    filt = (
        OptionsSnapshotRow.day_volume >= volume_min,
        OptionsSnapshotRow.open_interest >= 1,
    )
    q = select(OptionsSnapshotRow).where(and_(*filt))
    if symbol and symbol.strip():
        q = q.where(OptionsSnapshotRow.underlying_ticker == symbol.strip().upper())
    q = q.order_by(OptionsSnapshotRow.day_volume.desc()).limit(8000)
    rows = db.execute(q).scalars().all()
    scored = score_snapshot_rows(rows, min_score=min_score)

    def sort_key(rec: dict[str, object]) -> float:
        if sort_by == "score":
            return float(rec.get("score") or 0)
        if sort_by == "estimated_flow":
            return float(rec.get("estimatedFlowUsd") or 0)
        if sort_by == "volume":
            return float(rec.get("volume") or 0)
        return float(rec.get("strike_price") or 0)

    scored.sort(key=sort_key, reverse=descending)
    total = len(scored)
    start_idx = max((page - 1), 0) * page_size
    return {
        "symbol_filter": symbol.strip().upper() if symbol else None,
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": scored[start_idx : start_idx + page_size],
    }


@router.get("/bars/{ticker}")
def get_option_bars(
    ticker: str,
    multiplier: int = Query(1),
    timespan: str = Query("day"),
    from_date: str = Query(...),
    to_date: str = Query(...),
    limit: int = Query(500, le=5000),
):
    """Return OHLCV bars for an options contract from Massive API."""
    cfg = get_settings()
    if not cfg.massive_api_key:
        return {"ticker": ticker, "bars": [], "error": "massive_api_not_configured"}
    try:
        client = get_massive_client()
        data = client.get_bars(ticker, multiplier, timespan, from_date, to_date, limit=limit)
        results = data.get("results", [])
        bars = [
            {
                "timestamp": bar["t"],
                "open": bar["o"],
                "high": bar["h"],
                "low": bar["l"],
                "close": bar["c"],
                "volume": bar.get("v", 0),
                "vwap": bar.get("vw", bar["c"]),
            }
            for bar in results
        ]
        return {"ticker": ticker, "bars": bars, "count": len(bars)}
    except Exception as exc:
        logger.warning("Options bars failed for %s: %s", ticker, exc)
        return {"ticker": ticker, "bars": [], "error": str(exc)}


@router.get("/atm-history/{symbol}")
def get_atm_option_history(
    symbol: str,
    expiration: str = Query(...),
    contract_type: str = Query("call"),
    days_back: int = Query(60, le=365),
    db: Session = Depends(db_session_dep),
):
    """Return OHLCV bars for the ATM option of a given symbol + expiration."""
    sym = symbol.upper()
    from sqlalchemy import func

    spot = None
    spot_row = db.execute(
        select(OptionsSnapshotRow.underlying_price)
        .where(OptionsSnapshotRow.underlying_ticker == sym)
        .limit(1)
    ).scalar()
    if spot_row:
        spot = float(spot_row)
    else:
        try:
            cfg = get_settings()
            if cfg.fmp_api_key:
                from app.clients.fmp_client import get_fmp_client
                q = get_fmp_client().get_quote(sym)
                if q and q.get("price"):
                    spot = float(q["price"])
        except Exception:
            pass

    if not spot:
        # Last resort: use midpoint from any contract in chain
        try:
            from sqlalchemy import func as sa_func
            mid = db.execute(
                select(sa_func.avg(OptionsSnapshotRow.midpoint))
                .where(OptionsSnapshotRow.underlying_ticker == sym)
            ).scalar()
            if mid:
                spot = float(mid)
        except Exception:
            pass

    if not spot:
        return {"symbol": sym, "bars": [], "error": "no_spot_price"}

    contract = db.execute(
        select(OptionsSnapshotRow)
        .where(
            and_(
                OptionsSnapshotRow.underlying_ticker == sym,
                OptionsSnapshotRow.expiration_date == expiration,
                OptionsSnapshotRow.contract_type == contract_type,
            )
        )
        .order_by(func.abs(OptionsSnapshotRow.strike_price - spot))
        .limit(1)
    ).scalar_one_or_none()
    if not contract:
        return {"symbol": sym, "bars": [], "error": "no_contract_found"}

    options_ticker = contract.ticker
    cfg = get_settings()
    if not cfg.massive_api_key:
        return {"ticker": options_ticker, "bars": [], "error": "massive_api_not_configured"}

    import datetime as dt
    today = dt.datetime.now(dt.timezone.utc)
    from_date = (today - dt.timedelta(days=days_back)).strftime("%Y-%m-%d")
    to_date = today.strftime("%Y-%m-%d")

    try:
        client = get_massive_client()
        data = client.get_bars(options_ticker, 1, "day", from_date, to_date, limit=days_back)
        results = data.get("results", [])
        bars = [
            {
                "timestamp": bar["t"],
                "open": bar["o"],
                "high": bar["h"],
                "low": bar["l"],
                "close": bar["c"],
                "volume": bar.get("v", 0),
                "vwap": bar.get("vw", bar["c"]),
            }
            for bar in results
        ]
        return {
            "symbol": sym,
            "ticker": options_ticker,
            "strike": float(contract.strike_price),
            "expiration": str(contract.expiration_date),
            "contract_type": contract_type,
            "underlying_price": spot,
            "bars": bars,
            "count": len(bars),
        }
    except Exception as exc:
        logger.warning("ATM history failed for %s: %s", sym, exc)
        return {"symbol": sym, "bars": [], "error": str(exc)}
