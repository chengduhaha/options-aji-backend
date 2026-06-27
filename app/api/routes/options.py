"""Options API routes — chain, snapshots, GEX, unusual activity."""
from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, distinct, func, select
from sqlalchemy.orm import Session

from app.api.deps_membership import get_v3_access
from app.services.membership import V3Access

from app.analytics.gex_compute import compute_gex_profile, compute_gex_profile_from_contracts
from app.analytics.gex_history import list_gex_history, record_gex_snapshot, seed_price_closes
from app.analytics.iv_metrics import hv_series_and_current, iv_rank_percentile_proxy
from app.analytics.options_pricing import StrategyLegIn, evaluate_multi_leg
from app.analytics.unusual_v2 import score_snapshot_rows
from app.clients.fmp_client import get_fmp_client
from app.clients.futu_client import get_futu_client
from app.clients.massive_client import get_massive_client
from app.config import get_settings
from app.db.models import OptionsSnapshotRow
from app.db.session import SessionLocal, db_session_dep
from app.services.cache_service import (
    TTL_HOT, cache_get, cache_set,
    key_options_chain, key_gex,
)
from app.services.options_leaderboard import (
    get_leaderboard,
    get_unusual_leaderboard_page,
    refresh_all_leaderboards_cache,
    refresh_leaderboard_cache,
    refresh_unusual_leaderboard_cache,
)
from app.services.v3_board_access import apply_leaderboard_access, enforce_gex_symbol_access
from app.tools.openbb_tools import build_default_toolkit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/options", tags=["options"])


def _query_default(value, fallback):
    if value.__class__.__module__ == "fastapi.params" and hasattr(value, "default"):
        return fallback if value.default is None else value.default
    return value


def _compute_gex_profile_from_db(sym: str, *, limit: int) -> dict[str, object] | None:
    with SessionLocal() as session:
        rows = session.execute(
            select(OptionsSnapshotRow)
            .where(OptionsSnapshotRow.underlying_ticker == sym)
            .where(OptionsSnapshotRow.open_interest > 0)
            .order_by(OptionsSnapshotRow.day_volume.desc())
            .limit(limit)
        ).scalars().all()
    if not rows:
        return None
    spot = 0.0
    contracts: list[dict[str, object]] = []
    for row in rows:
        if spot <= 0 and isinstance(getattr(row, "underlying_price", None), (int, float)):
            spot = float(row.underlying_price)
        exp = getattr(row, "expiration_date", None)
        contracts.append(
            {
                "ticker": getattr(row, "ticker", None),
                "underlying": getattr(row, "underlying_ticker", sym),
                "contract_type": getattr(row, "contract_type", None),
                "expiration_date": str(exp) if exp else None,
                "strike_price": getattr(row, "strike_price", None),
                "gamma": getattr(row, "gamma", None),
                "open_interest": getattr(row, "open_interest", None),
                "implied_volatility": getattr(row, "implied_volatility", None),
            }
        )
    if spot <= 0:
        return None
    result = compute_gex_profile_from_contracts(sym, contracts=contracts, spot=spot)
    if result.get("error"):
        return None
    result["source"] = "database_gamma_estimate"
    result["spotSource"] = "options_snapshots"
    return result


@router.get("/chain/{symbol}")
def get_options_chain(
    symbol: str,
    expiration_date: Optional[str] = Query(None),
    contract_type: Optional[str] = Query(None),
    strike_min: Optional[float] = Query(None),
    strike_max: Optional[float] = Query(None),
    limit: int = Query(500, le=1000),
    realtime: bool = Query(False, description="Prefer low-latency Futu snapshot and bypass stale cache."),
    strike_window_pct: Optional[float] = Query(None, ge=0.01, le=1.0),
    db: Session = Depends(db_session_dep),
):
    """Return options chain from realtime Futu, Redis/DB, or Massive fallback."""
    sym = symbol.upper()
    expiration_date = _query_default(expiration_date, None)
    contract_type = _query_default(contract_type, None)
    strike_min = _query_default(strike_min, None)
    strike_max = _query_default(strike_max, None)
    limit = int(_query_default(limit, 500))
    realtime = bool(_query_default(realtime, False))
    strike_window_pct = _query_default(strike_window_pct, None)
    cfg = get_settings()
    cache_suffix = ""
    if contract_type:
        cache_suffix += f":{contract_type.lower()}"
    if strike_min is not None or strike_max is not None:
        cache_suffix += f":{strike_min or ''}-{strike_max or ''}"
    if strike_window_pct is not None:
        cache_suffix += f":spotwin{strike_window_pct}"
    cache_key = key_options_chain(sym, expiration_date or "") + cache_suffix

    # Non-realtime page loads should stay cache/DB-first. Live Futu calls are
    # comparatively slow and can saturate the single API worker under fan-out.
    cached = None if realtime else cache_get(cache_key)
    if cached:
        return cached

    if realtime and getattr(cfg, "futu_enabled", False):
        try:
            futu = get_futu_client()
            futu_strike_min = strike_min
            futu_strike_max = strike_max
            if strike_window_pct is not None and futu_strike_min is None and futu_strike_max is None:
                quote = futu.get_stock_quote(sym)
                spot = quote.get("last_price") if isinstance(quote, dict) else None
                if isinstance(spot, (int, float)) and spot > 0:
                    futu_strike_min = round(float(spot) * (1.0 - float(strike_window_pct)), 4)
                    futu_strike_max = round(float(spot) * (1.0 + float(strike_window_pct)), 4)
            result = futu.get_option_chain_snapshot(
                sym,
                expiration_date=expiration_date,
                contract_type=contract_type,
                strike_price_gte=futu_strike_min,
                strike_price_lte=futu_strike_max,
                limit=limit,
            )
            if isinstance(result, dict) and not result.get("error") and result.get("contracts"):
                cache_set(cache_key, result, ttl=cfg.futu_cache_ttl_seconds if realtime else TTL_HOT)
                return result
        except Exception as exc:
            logger.warning("Futu options chain failed for %s: %s", sym, exc)

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


@router.get("/iv/{symbol}")
def get_iv_analysis(symbol: str) -> dict[str, object]:
    """Return volatility analytics for a symbol (IV rank/proxy, skew, term)."""
    sym = symbol.strip().upper()
    if not sym:
        return {"error": "empty_symbol"}

    tk = build_default_toolkit()
    bar = tk.frontend_market_bar(sym)
    hv_series, hv_meta = hv_series_and_current(sym)
    atm_iv = bar.get("atmIv")
    hv_vals = [v for _, v in hv_series]
    rank_est, pct_est, note = iv_rank_percentile_proxy(
        current_iv_pct=float(atm_iv) if isinstance(atm_iv, (int, float)) else 0.0,
        hv_series_pct=hv_vals,
    )

    term: list[dict[str, object]] = []
    skew: list[dict[str, object]] = []
    try:
        chain = tk.get_option_chain_full(sym, prefetch_expirations=8)
        if not isinstance(chain, dict):
            raise TypeError("chain_payload_not_dict")
        prefetched = list(chain.get("prefetchedChains") or [])
        spot_val = chain.get("underlyingPrice")
        spot = float(spot_val) if isinstance(spot_val, (int, float)) else 0.0

        for block in prefetched[:8]:
            exp_str = str(block.get("expiration") or "")
            exp_calls = list(block.get("calls") or [])
            if not exp_calls:
                continue
            spot_use = spot
            if spot_use <= 0:
                strike0 = exp_calls[0].get("strike")
                if isinstance(strike0, (int, float)):
                    spot_use = float(strike0)
            best = None
            best_dist = float("inf")
            for rec in exp_calls:
                strike = rec.get("strike")
                iv = rec.get("impliedVolatility")
                if not isinstance(strike, (int, float)) or not isinstance(iv, (int, float)):
                    continue
                if iv <= 0:
                    continue
                dist = abs(float(strike) - spot_use)
                if dist < best_dist:
                    best_dist = dist
                    best = float(iv) * 100.0
            term.append({"expiration": exp_str, "atmIvPct": round(best, 4) if best is not None else None})

        calls_front = list(chain.get("calls") or [])
        if isinstance(calls_front, list):
            for rec in calls_front:
                if not isinstance(rec, dict):
                    continue
                strike = rec.get("strike")
                iv = rec.get("impliedVolatility")
                if not isinstance(strike, (int, float)) or not isinstance(iv, (int, float)):
                    continue
                if math.isnan(float(iv)) or float(iv) <= 0:
                    continue
                skew.append({"strike": float(strike), "ivPct": round(float(iv) * 100.0, 4)})
        skew.sort(key=lambda x: float(x["strike"]))
    except Exception as exc:
        logger.warning("iv chain bundle failed symbol=%s err=%s", sym, exc)

    return {
        "symbol": sym,
        "atmIvPct": atm_iv,
        "ivRank": rank_est,
        "ivPercentile": pct_est,
        "methodology": note,
        "hvMeta": hv_meta,
        "hvSeries": [{"date": d, "hv20": v} for d, v in hv_series[-260:]],
        "termStructure": term,
        "skew": skew[:80],
    }


@router.get("/expirations/{symbol}")
def get_expirations(symbol: str, db: Session = Depends(db_session_dep)):
    """Return available expiration dates for a symbol."""
    sym = symbol.upper()
    exps = db.execute(
        select(distinct(OptionsSnapshotRow.expiration_date))
        .where(OptionsSnapshotRow.underlying_ticker == sym)
        .order_by(OptionsSnapshotRow.expiration_date)
    ).scalars().all()
    return {"symbol": sym, "expirations": [str(e) for e in exps if e]}


@router.get("/gex/{symbol}")
def get_gex(
    symbol: str,
    realtime: bool = Query(False, description="Use live Futu quote and option Greeks when available."),
    limit: int = Query(500, ge=50, le=1000),
    strike_window_pct: float = Query(0.2, ge=0.05, le=1.0),
    access: V3Access = Depends(get_v3_access),
):
    """Return Gamma Exposure profile (from cache, upstream, or local compute)."""
    sym = symbol.upper()
    enforce_gex_symbol_access(sym, access)
    realtime = bool(_query_default(realtime, False))
    limit = int(_query_default(limit, 500))
    strike_window_pct = float(_query_default(strike_window_pct, 0.2))
    cfg = get_settings()
    if realtime and getattr(cfg, "futu_enabled", False):
        rt_key = f"{key_gex(sym)}:realtime:{strike_window_pct}:{limit}"
        cached_rt = cache_get(rt_key)
        if isinstance(cached_rt, dict) and isinstance(cached_rt.get("netGex"), (int, float)):
            return cached_rt
        try:
            futu = get_futu_client()
            quote = futu.get_stock_quote(sym)
            spot = quote.get("last_price") if isinstance(quote, dict) else None
            if isinstance(spot, (int, float)) and spot > 0:
                chain = futu.get_option_chain_snapshot(
                    sym,
                    strike_price_gte=round(float(spot) * (1.0 - strike_window_pct), 4),
                    strike_price_lte=round(float(spot) * (1.0 + strike_window_pct), 4),
                    limit=limit,
                )
                contracts = chain.get("contracts") if isinstance(chain, dict) else None
                if isinstance(contracts, list) and contracts:
                    result = compute_gex_profile_from_contracts(sym, contracts=contracts, spot=float(spot))
                    if not result.get("error"):
                        cache_set(rt_key, result, ttl=cfg.futu_cache_ttl_seconds)
                        record_gex_snapshot(sym, dict(result))
                        return result
        except Exception as exc:
            logger.warning("Realtime Futu GEX failed for %s: %s", sym, exc)

    cached = cache_get(key_gex(sym))
    if cached:
        if isinstance(cached, dict) and isinstance(cached.get("netGex"), (int, float)):
            record_gex_snapshot(sym, dict(cached))
        return cached

    result = _compute_gex_profile_from_db(sym, limit=limit)
    if result is None:
        result = compute_gex_profile(sym)
    if not result.get("error"):
        cache_set(key_gex(sym), result, ttl=TTL_HOT)
        record_gex_snapshot(sym, dict(result))
    return result


@router.get("/gex/history/{symbol}")
def get_gex_history_endpoint(
    symbol: str,
    days: int = Query(120, ge=10, le=400),
    access: V3Access = Depends(get_v3_access),
):
    """Sparse GEX points from Redis snapshots + Yahoo daily closes."""

    sym = symbol.upper()
    enforce_gex_symbol_access(sym, access)
    cached = cache_get(key_gex(sym))
    if isinstance(cached, dict) and isinstance(cached.get("netGex"), (int, float)):
        record_gex_snapshot(sym, dict(cached))

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


@router.get("/leaderboard/{board}")
def get_options_leaderboard(
    board: str,
    refresh: bool = Query(False, description="Force refresh from Futu (admin/debug)"),
    access: V3Access = Depends(get_v3_access),
):
    """Full cached leaderboard for client-side filtering (15-min TTL)."""
    payload = get_leaderboard(board, force_refresh=refresh)
    board_id = str(payload.get("board") or board)
    return apply_leaderboard_access(payload, board_id=board_id, access=access)


@router.post("/leaderboard/refresh")
def refresh_all_leaderboards_endpoint():
    """Manual refresh for all option leaderboards."""
    payload = refresh_all_leaderboards_cache()
    errors = [bid for bid, row in payload.items() if isinstance(row, dict) and row.get("error")]
    if errors and len(errors) == len(payload):
        raise HTTPException(status_code=503, detail=f"all_boards_failed:{','.join(errors)}")
    return {"boards": payload, "refreshed_at": payload.get("unusual", {}).get("synced_at")}


@router.post("/leaderboard/{board}/refresh")
def refresh_leaderboard_endpoint(board: str):
    """Manual cache refresh for one leaderboard board."""
    payload = refresh_leaderboard_cache(board)  # type: ignore[arg-type]
    if payload.get("error"):
        raise HTTPException(status_code=503, detail=payload["error"])
    return payload


@router.get("/unusual-leaderboard")
def get_unusual_leaderboard(
    page: int = Query(1, ge=1, le=10),
    limit: int = Query(10, ge=1, le=10),
    vol_oi_min: float = Query(3.0, ge=0),
    volume_min: int = Query(500, ge=0),
    refresh: bool = Query(False, description="Force refresh from Futu (admin/debug)"),
):
    """Top 100 unusual US options from Futu get_option_screen; paginated 10 per page."""
    return get_unusual_leaderboard_page(
        page=page,
        page_size=limit,
        vol_oi_min=vol_oi_min,
        volume_min=volume_min,
        force_refresh=refresh,
    )


@router.post("/unusual-leaderboard/refresh")
def refresh_unusual_leaderboard_endpoint():
    """Manual cache refresh for unusual leaderboard."""
    payload = refresh_unusual_leaderboard_cache()
    if payload.get("error"):
        raise HTTPException(status_code=503, detail=payload["error"])
    return payload


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


class StrategyLeg(BaseModel):
    side: str = Field(pattern="^(buy|sell)$")
    option_type: str = Field(pattern="^(call|put)$")
    strike: float = Field(gt=0)
    premium: float = Field(ge=0)
    contracts: int = Field(default=1, ge=1, le=10000)
    days_to_expiry: float = Field(default=30.0, ge=0.01, le=3650.0)
    iv: float = Field(default=0.35, ge=0.01, le=5.0)


class StrategyEvaluateRequest(BaseModel):
    symbol: str
    spot: float = Field(gt=0)
    risk_free_rate: float = Field(default=0.0525, ge=0.0, le=0.2)
    legs: list[StrategyLeg] = Field(min_length=1)
    spot_grid_pct: list[float] = Field(
        default_factory=lambda: [-25, -15, -10, -5, 0, 5, 10, 15, 25],
    )


@router.post("/strategy")
def evaluate_strategy_v2(body: StrategyEvaluateRequest) -> dict[str, object]:
    """Evaluate option strategy under 2.0 API namespace."""
    sym = body.symbol.strip().upper()
    legs_in: list[StrategyLegIn] = [
        StrategyLegIn(
            side=leg.side,
            option_type=leg.option_type,
            strike=float(leg.strike),
            premium=float(leg.premium),
            contracts=int(leg.contracts),
            days_to_expiry=float(leg.days_to_expiry),
            iv=float(leg.iv),
        )
        for leg in body.legs
    ]
    try:
        out = evaluate_multi_leg(
            spot=float(body.spot),
            risk_free=float(body.risk_free_rate),
            legs=legs_in,
            spot_moves_pct=[float(x) for x in body.spot_grid_pct],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"symbol": sym, **out}


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
                q = get_fmp_client().get_quote(sym)
                if q and q.get("price"):
                    spot = float(q["price"])
        except Exception:
            pass

    if not spot:
        # Last resort: use midpoint from any contract in chain
        try:
            mid = db.execute(
                select(func.avg(OptionsSnapshotRow.midpoint))
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
