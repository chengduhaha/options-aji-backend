"""Per-symbol detail endpoints for OptionsAji 2.0 (overview, volatility, unusual, GEX)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps_membership import get_v3_access
from app.analytics.gex_history import record_gex_snapshot
from app.services.membership import V3Access
from app.services.v3_board_access import enforce_gex_symbol_access
from app.analytics.iv_metrics import (
    hv_series_and_current,
    hv_series_and_meta_from_hist,
    iv_rank_percentile_proxy,
)
from app.analytics.unusual_v2 import (
    score_snapshot_rows,
    snapshot_rows_from_futu_contracts,
)
from app.api.deps import bearer_subscription_optional
from app.clients.futu_client import get_futu_client
from app.config import get_settings
from app.db.models import OptionsSnapshotRow
from app.db.session import db_session_dep
from app.services.cache_service import TTL_HOT, cache_get, cache_set, key_gex, key_stock_overview, key_stock_unusual_v2, key_stock_volatility
from app.tools.openbb_tools import build_default_toolkit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock", tags=["stock"])


def _overview_toolkit_block(sym: str) -> tuple[dict[str, object], dict[str, object]]:
    tk = build_default_toolkit()
    return tk.frontend_market_bar(sym), tk.get_quote(sym)


def _fetch_history_1y(sym: str) -> object:
    from app.tools.stock_history import fetch_daily_stock_history

    hist, _source = fetch_daily_stock_history(sym, count=280)
    return hist


def _ohlc_from_hist(hist: object) -> list[dict[str, object]]:
    ohlc: list[dict[str, object]] = []
    if hist is None or getattr(hist, "empty", True):
        return ohlc
    try:
        for idx, row in hist.iterrows():
            d = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]
            ohlc.append(
                {
                    "date": d,
                    "open": float(row["Open"]) if "Open" in row else None,
                    "high": float(row["High"]) if "High" in row else None,
                    "low": float(row["Low"]) if "Low" in row else None,
                    "close": float(row["Close"]) if "Close" in row else None,
                    "volume": float(row["Volume"]) if "Volume" in row else None,
                }
            )
    except Exception as exc:
        logger.warning("ohlc from hist: %s", exc)
    return ohlc


def _mid_price(last: object, bid: object, ask: object) -> Optional[float]:
    cand: list[float] = []
    if isinstance(last, (int, float)) and not (isinstance(last, float) and math.isnan(last)) and last > 0:
        cand.append(float(last))
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and ask > 0:
        cand.append((float(bid) + float(ask)) / 2)
    return cand[0] if cand else None


def _futu_overview_chain(symbol: str) -> list[dict[str, object]]:
    payload = get_futu_client().get_option_chain_snapshot(symbol, limit=2500)
    return list(payload.get("contracts") or []) if isinstance(payload, dict) else []


def _expected_moves_from_futu_contracts(contracts: list[dict[str, object]], spot: float) -> list[dict[str, object]]:
    if spot <= 0 or not contracts:
        return []

    by_exp: dict[str, list[dict[str, object]]] = {}
    for rec in contracts:
        if not isinstance(rec, dict):
            continue
        exp = str(rec.get("expiration_date") or "")[:10]
        if exp:
            by_exp.setdefault(exp, []).append(rec)

    out: list[dict[str, object]] = []
    today = dt.datetime.now(dt.timezone.utc).date()
    for label, max_idx in (("this_week", 6), ("next_week", 14), ("monthly", 180)):
        pick: str | None = None
        for exp in sorted(by_exp.keys()):
            try:
                d = dt.date.fromisoformat(exp)
                days = (d - today).days
                if 0 <= days <= max_idx:
                    pick = exp
            except ValueError:
                continue
        if pick is None:
            continue
        calls = [c for c in by_exp[pick] if str(c.get("contract_type")).lower() == "call"]
        puts = [c for c in by_exp[pick] if str(c.get("contract_type")).lower() == "put"]
        if not calls or not puts:
            continue
        call_row = min(calls, key=lambda c: abs(float(c.get("strike_price") or 0) - spot))
        put_row = min(puts, key=lambda c: abs(float(c.get("strike_price") or 0) - spot))
        cm = _mid_price(call_row.get("last_trade_price"), call_row.get("bid"), call_row.get("ask"))
        pm = _mid_price(put_row.get("last_trade_price"), put_row.get("bid"), put_row.get("ask"))
        if cm is None or pm is None:
            continue
        straddle = cm + pm
        bucket_zh = {"this_week": "本周到期", "next_week": "下周窗口", "monthly": "近月到期"}.get(label, label)
        out.append(
            {
                "bucket": label,
                "bucketZh": bucket_zh,
                "expiration": pick,
                "straddleUsd": round(straddle, 4),
                "pct": round(straddle / spot * 100.0, 4),
                "spot": round(spot, 4),
                "atmStrike": round(float(call_row.get("strike_price") or 0), 4),
                "callMid": round(cm, 4),
                "putMid": round(pm, 4),
            }
        )
    return out


def _expected_moves_from_futu(symbol: str, spot: float) -> list[dict[str, object]]:
    if spot <= 0:
        return []
    return _expected_moves_from_futu_contracts(_futu_overview_chain(symbol), spot)


def _option_liquidity_from_futu_contracts(contracts: list[dict[str, object]]) -> tuple[float, float, float, float]:
    if not contracts:
        return 0.0, 0.0, 0.0, 0.0
    by_exp: dict[str, list[dict[str, object]]] = {}
    for rec in contracts:
        if not isinstance(rec, dict):
            continue
        exp = str(rec.get("expiration_date") or "")[:10]
        if exp:
            by_exp.setdefault(exp, []).append(rec)
    if not by_exp:
        return 0.0, 0.0, 0.0, 0.0
    nearest = sorted(by_exp.keys())[0]
    call_vol = put_vol = call_oi = put_oi = 0.0
    for rec in by_exp[nearest]:
        side = str(rec.get("contract_type") or "").lower()
        vol = float(rec.get("day_volume") or 0)
        oi = float(rec.get("open_interest") or 0)
        if side == "call":
            call_vol += vol
            call_oi += oi
        elif side == "put":
            put_vol += vol
            put_oi += oi
    return call_vol, put_vol, call_oi, put_oi


def _option_liquidity_from_futu(symbol: str) -> tuple[float, float, float, float]:
    return _option_liquidity_from_futu_contracts(_futu_overview_chain(symbol))


def _overview_followups(sym: str, spot: float, hist: object) -> tuple[
    list[tuple[str, float]],
    dict[str, object],
    list[dict[str, object]],
    float,
    float,
    float,
    float,
    list[dict[str, object]],
]:
    hv_series, hv_meta = hv_series_and_meta_from_hist(hist, sym)
    ohlc = _ohlc_from_hist(hist)
    cfg = get_settings()
    if getattr(cfg, "futu_enabled", False):
        futu_contracts = _futu_overview_chain(sym)
        call_vol, put_vol, call_oi, put_oi = _option_liquidity_from_futu_contracts(futu_contracts)
        expected_moves = _expected_moves_from_futu_contracts(futu_contracts, spot)
    else:
        from app.tools.yf_helpers import yf_ticker

        call_vol = put_vol = call_oi = put_oi = 0.0
        expected_moves: list[dict[str, object]] = []
        t = yf_ticker(sym)
        try:
            opts = list(t.options or [])
            if opts and spot > 0:
                oc = t.option_chain(opts[0])
                if not oc.calls.empty:
                    call_vol = float(oc.calls["volume"].fillna(0).astype(float).sum())
                    call_oi = float(oc.calls["openInterest"].fillna(0).astype(float).sum())
                if not oc.puts.empty:
                    put_vol = float(oc.puts["volume"].fillna(0).astype(float).sum())
                    put_oi = float(oc.puts["openInterest"].fillna(0).astype(float).sum())
        except Exception as exc:
            logger.warning("stock overview opt stats %s: %s", sym, exc)
        expected_moves = _expected_moves_from_yfinance(sym, spot, ticker=t)
    return hv_series, hv_meta, ohlc, call_vol, put_vol, call_oi, put_oi, expected_moves


def _expected_moves_from_yfinance(
    symbol: str,
    spot: float,
    ticker: object | None = None,
) -> list[dict[str, object]]:
    from app.tools.yf_helpers import yf_ticker

    t = ticker if ticker is not None else yf_ticker(symbol)
    try:
        opts = list(t.options or [])
    except Exception:
        opts = []
    out: list[dict[str, object]] = []
    if not opts or spot <= 0:
        return out
    for label, max_idx in (("this_week", 6), ("next_week", 14), ("monthly", 180)):
        pick = None
        for exp in opts:
            try:
                parts = [int(x) for x in str(exp).split("-")]
                d = dt.date(parts[0], parts[1], parts[2])
                days = (d - dt.datetime.now(dt.timezone.utc).date()).days
                if 0 <= days <= max_idx:
                    pick = exp
            except Exception:
                continue
        if pick is None:
            continue
        try:
            oc = t.option_chain(pick)
            row_c = oc.calls.loc[(oc.calls["strike"].astype(float) - spot).abs().idxmin()]
            row_p = oc.puts.loc[(oc.puts["strike"].astype(float) - spot).abs().idxmin()]
            cm = _mid_price(row_c.get("lastPrice"), row_c.get("bid"), row_c.get("ask"))
            pm = _mid_price(row_p.get("lastPrice"), row_p.get("bid"), row_p.get("ask"))
            if cm is None or pm is None:
                continue
            straddle = cm + pm
            bucket_zh = {"this_week": "本周到期", "next_week": "下周窗口", "monthly": "近月到期"}.get(
                label, label
            )
            out.append(
                {
                    "bucket": label,
                    "bucketZh": bucket_zh,
                    "expiration": str(pick),
                    "straddleUsd": round(straddle, 4),
                    "pct": round(straddle / spot * 100.0, 4),
                    "spot": round(spot, 4),
                    "atmStrike": round(float(row_c["strike"]), 4),
                    "callMid": round(cm, 4),
                    "putMid": round(pm, 4),
                }
            )
        except Exception as exc:
            logger.debug("expected move %s %s: %s", symbol, pick, exc)
    return out


def _volatility_bar_term_skew(sym: str) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
    tk = build_default_toolkit()
    bar = tk.frontend_market_bar(sym)
    term: list[dict[str, object]] = []
    skew: list[dict[str, object]] = []
    cfg = get_settings()

    if getattr(cfg, "futu_enabled", False):
        try:
            quote = get_futu_client().get_stock_quote(sym)
            spot = float(quote.get("last_price") or 0) if isinstance(quote, dict) else 0.0
            payload = get_futu_client().get_option_chain_snapshot(sym, limit=2500)
            contracts = list(payload.get("contracts") or []) if isinstance(payload, dict) else []
            by_exp: dict[str, list[dict[str, object]]] = {}
            for rec in contracts:
                if not isinstance(rec, dict):
                    continue
                exp = str(rec.get("expiration_date") or "")[:10]
                if exp:
                    by_exp.setdefault(exp, []).append(rec)
            for exp in sorted(by_exp.keys())[:8]:
                calls = [c for c in by_exp[exp] if str(c.get("contract_type")).lower() == "call"]
                if not calls or spot <= 0:
                    continue
                atm = min(calls, key=lambda c: abs(float(c.get("strike_price") or 0) - spot))
                iv = atm.get("implied_volatility")
                iv_pct = None
                if isinstance(iv, (int, float)) and float(iv) > 0:
                    iv_f = float(iv)
                    iv_pct = iv_f * 100.0 if iv_f <= 2.5 else iv_f
                term.append({"expiration": exp, "atmIvPct": iv_pct})
            ch = tk.get_option_chain_full(sym)
            exp = str(ch.get("expiration") or "")
            for rec in list(ch.get("calls") or []):
                if not isinstance(rec, dict):
                    continue
                iv = rec.get("impliedVolatility")
                if isinstance(iv, (int, float)) and iv > 0:
                    skew.append({"strike": rec.get("strike"), "ivPct": float(iv) * 100.0})
            skew.sort(key=lambda x: float(x.get("strike") or 0))
            if exp and not term:
                term.append({"expiration": exp, "atmIvPct": bar.get("atmIv")})
        except Exception as exc:
            logger.warning("volatility futu %s: %s", sym, exc)
        return bar, term, skew

    try:
        from app.tools.yf_helpers import yf_ticker

        t = yf_ticker(sym)
        opts = list(t.options or [])
        qi = t.fast_info
        spot = float(qi.get("last_price") or 0) if isinstance(qi.get("last_price"), (int, float)) else 0.0
        for exp in opts[:8]:
            try:
                oc = t.option_chain(exp)
                calls = oc.calls
                if calls.empty or "strike" not in calls.columns:
                    continue
                idx = (
                    (calls["strike"].astype(float) - spot).abs().idxmin()
                    if spot > 0
                    else calls["strike"].astype(float).idxmin()
                )
                row = calls.loc[idx]
                iv_r = row.get("impliedVolatility")
                iv_pct = float(iv_r) * 100.0 if isinstance(iv_r, (int, float)) and iv_r > 0 else None
                term.append({"expiration": str(exp), "atmIvPct": iv_pct})
            except Exception:
                continue
    except Exception as exc:
        logger.warning("term structure %s: %s", sym, exc)

    try:
        ch = tk.get_option_chain_full(sym)
        calls = ch.get("calls") or []
        if isinstance(calls, list):
            for rec in calls:
                if not isinstance(rec, dict):
                    continue
                iv = rec.get("impliedVolatility")
                if isinstance(iv, (int, float)) and iv > 0:
                    skew.append({"strike": rec.get("strike"), "ivPct": float(iv) * 100.0})
            skew.sort(key=lambda x: float(x.get("strike") or 0))
    except Exception as exc:
        logger.warning("skew %s: %s", sym, exc)

    return bar, term, skew


@router.get("/{symbol}/overview")
async def stock_overview(
    symbol: str,
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    sym = symbol.strip().upper()
    cached = cache_get(key_stock_overview(sym))
    if cached:
        return cached

    (bar, qt), hist = await asyncio.gather(
        asyncio.to_thread(_overview_toolkit_block, sym),
        asyncio.to_thread(_fetch_history_1y, sym),
    )
    spot = float(bar.get("price") or 0) if isinstance(bar.get("price"), (int, float)) else 0.0

    (
        hv_series,
        hv_meta,
        ohlc,
        call_vol,
        put_vol,
        call_oi,
        put_oi,
        expected_moves,
    ) = await asyncio.to_thread(_overview_followups, sym, spot, hist)

    pcr_vol = put_vol / call_vol if call_vol > 0 else None
    pcr_oi = put_oi / call_oi if call_oi > 0 else None

    atm_iv = bar.get("atmIv")
    iv_rank = bar.get("ivRank")
    iv_pct = bar.get("ivPercentile")
    hv20 = hv_meta.get("hv20")
    hv60 = hv_meta.get("hv60")
    iv_hv = None
    if isinstance(atm_iv, (int, float)) and isinstance(hv20, (int, float)) and hv20 and hv20 > 0:
        iv_hv = float(atm_iv) / float(hv20)

    result = {
        "symbol": sym,
        "quote": qt,
        "bar": bar,
        "hvMeta": hv_meta,
        "hvSeries": [{"date": d, "hv20": v} for d, v in hv_series[-260:]],
        "priceSeries": ohlc[-400:],
        "optionLiquidity": {
            "callVolume": call_vol,
            "putVolume": put_vol,
            "callOpenInterest": call_oi,
            "putOpenInterest": put_oi,
            "pcrVolume": pcr_vol,
            "pcrOpenInterest": pcr_oi,
        },
        "keyStats": {
            "atmIv": atm_iv,
            "ivRank": iv_rank,
            "ivPercentile": iv_pct,
            "ivMethodology": bar.get("ivMethodology"),
            "hv20": hv20,
            "hv60": hv60,
            "ivHvRatio": round(iv_hv, 4) if iv_hv is not None else None,
        },
        "expectedMoves": expected_moves,
    }
    cache_set(key_stock_overview(sym), result, ttl=TTL_HOT)
    return result


@router.get("/{symbol}/volatility")
async def stock_volatility(
    symbol: str,
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    sym = symbol.strip().upper()
    vol_key = key_stock_volatility(sym)
    if cached := cache_get(vol_key):
        if isinstance(cached, dict):
            return cached

    (hv_series, hv_meta), (bar, term, skew) = await asyncio.gather(
        asyncio.to_thread(hv_series_and_current, sym),
        asyncio.to_thread(_volatility_bar_term_skew, sym),
    )
    atm_iv = bar.get("atmIv")
    hv_vals = [v for _, v in hv_series]
    rank_est, pct_est, note = iv_rank_percentile_proxy(
        current_iv_pct=float(atm_iv) if isinstance(atm_iv, (int, float)) else 0.0,
        hv_series_pct=hv_vals,
    )

    result = {
        "symbol": sym,
        "ivVsHv": {"points": [{"date": d, "hv20": v} for d, v in hv_series[-260:]], "hvMeta": hv_meta},
        "gauges": {
            "atmIvPct": atm_iv,
            "ivRankProxy": rank_est,
            "ivPercentileProxy": pct_est,
            "methodology": note,
        },
        "termStructure": term,
        "skew": skew[:60],
        "bar": bar,
    }
    cache_set(vol_key, result, ttl=TTL_HOT)
    return result


@router.get("/{symbol}/unusual")
def stock_unusual(
    symbol: str,
    vol_oi_min: float = Query(default=3.0, ge=0),
    volume_min: float = Query(default=200.0, ge=0),
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    tk = build_default_toolkit()
    sym = symbol.strip().upper()
    ch = tk.get_option_chain_full(sym)
    rows: list[dict[str, object]] = []
    if not isinstance(ch, dict) or ch.get("error"):
        return {"symbol": sym, "items": [], "error": ch.get("error") if isinstance(ch, dict) else "no_chain"}
    exp = str(ch.get("expiration") or "")
    for side, key in (("call", "calls"), ("put", "puts")):
        arr = ch.get(key) or []
        if not isinstance(arr, list):
            continue
        for rec in arr:
            if not isinstance(rec, dict):
                continue
            vol = float(rec.get("volume") or 0)
            oi = float(rec.get("openInterest") or 0)
            if vol < volume_min or oi < 1:
                continue
            ratio = vol / max(oi, 1.0)
            if ratio < vol_oi_min:
                continue
            sentiment = "Bullish" if side == "call" else "Bearish"
            rows.append(
                {
                    "type": side,
                    "strike": rec.get("strike"),
                    "expiration": exp,
                    "volume": vol,
                    "openInterest": oi,
                    "volOiRatio": round(ratio, 4),
                    "ivPct": float(rec["impliedVolatility"]) * 100.0
                    if isinstance(rec.get("impliedVolatility"), (int, float))
                    else None,
                    "sentiment": sentiment,
                }
            )
    rows.sort(key=lambda r: float(r.get("volOiRatio") or 0), reverse=True)
    return {"symbol": sym, "items": rows[:80]}


@router.get("/{symbol}/unusual-v2")
def stock_unusual_v2(
    symbol: str,
    min_score: int = Query(default=60, ge=0, le=100),
    sort_by: str = Query(default="score"),
    order: str = Query(default="desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(db_session_dep),
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    """Multi-factor unusual score from PostgreSQL snapshots, with Futu on-demand fallback."""

    sym = symbol.strip().upper()
    if sort_by not in ("score", "estimated_flow", "volume", "strike"):
        sort_by = "score"
    descending = order.lower() != "asc"
    cache_suffix = f"{min_score}:{sort_by}:{order}:{page}:{page_size}"
    unusual_key = key_stock_unusual_v2(sym, cache_suffix)
    if cached := cache_get(unusual_key):
        if isinstance(cached, dict):
            return cached

    data_source = "database_snapshots"

    rows = db.execute(
        select(OptionsSnapshotRow)
        .where(
            OptionsSnapshotRow.underlying_ticker == sym,
            OptionsSnapshotRow.day_volume >= 30,
            OptionsSnapshotRow.open_interest >= 1,
        )
        .order_by(OptionsSnapshotRow.day_volume.desc())
        .limit(5000)
    ).scalars().all()

    if not rows and getattr(get_settings(), "futu_enabled", False):
        payload = get_futu_client().get_option_chain_snapshot(sym, limit=2500)
        contracts = list(payload.get("contracts") or []) if isinstance(payload, dict) else []
        rows = snapshot_rows_from_futu_contracts(contracts)
        data_source = "futu_on_demand"

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
    page_rows = scored[start_idx : start_idx + page_size]
    payload = {
        "symbol": sym,
        "source": data_source,
        "total": total,
        "page": page,
        "page_size": page_size,
        "sort_by": sort_by,
        "order": "desc" if descending else "asc",
        "items": page_rows,
    }
    cache_set(unusual_key, payload, ttl=300)
    return payload


@router.get("/{symbol}/gex")
def stock_gex(
    symbol: str,
    access: V3Access = Depends(get_v3_access),
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    from app.api.routes.options import _finalize_gex_response, _resolve_gex_profile

    sym = symbol.strip().upper()
    enforce_gex_symbol_access(sym, access)
    cached = cache_get(key_gex(sym))
    if isinstance(cached, dict) and isinstance(cached.get("netGex"), (int, float)):
        record_gex_snapshot(sym, dict(cached))
        return cached

    return _finalize_gex_response(sym, _resolve_gex_profile(sym, limit=500))
