"""Per-symbol detail endpoints for OptionsAji 2.0."""

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any, Optional

import yfinance as yf
from fastapi import APIRouter, Depends, Query

from app.analytics.earnings_depth import build_earnings_history
from app.analytics.iv_metrics import hv_series_and_current, iv_rank_percentile_proxy
from app.api.deps import bearer_subscription_optional
from app.config import get_settings
from app.tools.openbb_tools import build_default_toolkit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock", tags=["stock"])


@router.get("/{symbol}/overview")
def stock_overview(
    symbol: str,
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    tk = build_default_toolkit()
    sym = symbol.strip().upper()
    bar = tk.frontend_market_bar(sym)
    qt = tk.get_quote(sym)

    hv_series, hv_meta = hv_series_and_current(sym)
    spot = float(bar.get("price") or 0) if isinstance(bar.get("price"), (int, float)) else 0.0

    # Price history for chart
    ohlc: list[dict[str, object]] = []
    try:
        t = yf.Ticker(sym)
        hist = t.history(period="1y", interval="1d", auto_adjust=True)
        if hist is not None and not hist.empty:
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
        logger.warning("stock overview history %s: %s", sym, exc)

    # Options aggregates (front expiry)
    call_vol = put_vol = call_oi = put_oi = 0.0
    try:
        t2 = yf.Ticker(sym)
        opts = list(t2.options or [])
        if opts and spot > 0:
            oc = t2.option_chain(opts[0])
            if not oc.calls.empty:
                call_vol = float(oc.calls["volume"].fillna(0).astype(float).sum())
                call_oi = float(oc.calls["openInterest"].fillna(0).astype(float).sum())
            if not oc.puts.empty:
                put_vol = float(oc.puts["volume"].fillna(0).astype(float).sum())
                put_oi = float(oc.puts["openInterest"].fillna(0).astype(float).sum())
    except Exception as exc:
        logger.warning("stock overview opt stats %s: %s", sym, exc)

    pcr_vol = None
    if call_vol > 0:
        pcr_vol = put_vol / call_vol
    pcr_oi = None
    if call_oi > 0:
        pcr_oi = put_oi / call_oi

    atm_iv = bar.get("atmIv")
    iv_rank = bar.get("ivRank")
    iv_pct = bar.get("ivPercentile")
    hv20 = hv_meta.get("hv20")
    hv60 = hv_meta.get("hv60")
    iv_hv = None
    if isinstance(atm_iv, (int, float)) and isinstance(hv20, (int, float)) and hv20 and hv20 > 0:
        iv_hv = float(atm_iv) / float(hv20)

    expected_moves = _expected_moves_for_symbol(sym, spot)

    next_earn = None
    days_to = None
    try:
        t3 = yf.Ticker(sym)
        eds = getattr(t3, "earnings_dates", None)
        if eds is not None and hasattr(eds, "index") and len(eds.index) > 0:
            ts0 = eds.index[0]
            if hasattr(ts0, "date"):
                next_earn = ts0.date().isoformat()
                delta = ts0.date() - dt.datetime.now(dt.timezone.utc).date()
                days_to = delta.days
    except Exception:
        pass

    return {
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
        "earnings": {"nextDate": next_earn, "daysTo": days_to},
    }


def _expected_moves_for_symbol(symbol: str, spot: float) -> list[dict[str, object]]:
    t = yf.Ticker(symbol)
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
            lc = row_c.get("lastPrice")
            la = row_c.get("ask")
            lb = row_c.get("bid")
            pc = row_p.get("lastPrice")
            pa = row_p.get("ask")
            pb = row_p.get("bid")
            def mid(last: object, bid: object, ask: object) -> Optional[float]:
                cand: list[float] = []
                if isinstance(last, (int, float)) and not (isinstance(last, float) and math.isnan(last)) and last > 0:
                    cand.append(float(last))
                if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and ask > 0:
                    cand.append((float(bid) + float(ask)) / 2)
                return cand[0] if cand else None

            cm = mid(lc, lb, la)
            pm = mid(pc, pb, pa)
            if cm is None or pm is None:
                continue
            straddle = cm + pm
            pct = straddle / spot * 100.0
            out.append({"bucket": label, "expiration": str(pick), "straddleUsd": round(straddle, 4), "pct": round(pct, 4)})
        except Exception as exc:
            logger.debug("expected move %s %s: %s", symbol, pick, exc)
    return out


@router.get("/{symbol}/chain")
def stock_chain(
    symbol: str,
    expiration: Optional[str] = Query(default=None),
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    tk = build_default_toolkit()
    return tk.get_option_chain_full(symbol, expiration=expiration)


@router.get("/{symbol}/volatility")
def stock_volatility(
    symbol: str,
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    sym = symbol.strip().upper()
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
    try:
        t = yf.Ticker(sym)
        opts = list(t.options or [])
        qi = t.fast_info
        spot = float(qi.get("last_price") or 0) if isinstance(qi.get("last_price"), (int, float)) else 0.0
        for exp in opts[:8]:
            try:
                oc = t.option_chain(exp)
                calls = oc.calls
                if calls.empty or "strike" not in calls.columns:
                    continue
                idx = (calls["strike"].astype(float) - spot).abs().idxmin() if spot > 0 else calls["strike"].astype(float).idxmin()
                row = calls.loc[idx]
                iv_r = row.get("impliedVolatility")
                iv_pct = float(iv_r) * 100.0 if isinstance(iv_r, (int, float)) and iv_r > 0 else None
                term.append({"expiration": str(exp), "atmIvPct": iv_pct})
            except Exception:
                continue
    except Exception as exc:
        logger.warning("term structure %s: %s", sym, exc)

    skew: list[dict[str, object]] = []
    try:
        ch = tk.get_option_chain_full(sym)
        exp = str(ch.get("expiration") or "")
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

    return {
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


@router.get("/{symbol}/gex")
def stock_gex(
    symbol: str,
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    tk = build_default_toolkit()
    return tk.get_gex(symbol.strip().upper())


@router.get("/{symbol}/strategy-ideas")
def stock_strategy_ideas(
    symbol: str,
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    sym = symbol.strip().upper()
    bar = build_default_toolkit().frontend_market_bar(sym)
    iv_rank = bar.get("ivRank")
    high_iv = isinstance(iv_rank, (int, float)) and float(iv_rank) >= 60
    ideas: list[dict[str, str]] = []
    if high_iv:
        ideas.append(
            {"id": "iron_condor", "title": "Iron Condor", "note": "IV 偏高时可关注卖方价差组合（示例，非建议）。"}
        )
        ideas.append({"id": "credit_spread", "title": "Credit Spread", "note": "用垂直价差收权利金，控制最大亏损。"})
    else:
        ideas.append({"id": "long_call", "title": "Long Call / Call Spread", "note": "IV 不高时可偏多腿方向性买方。"})
        ideas.append({"id": "straddle", "title": "Straddle", "note": "博弈波动放大，注意 Theta 损耗。"})
    return {"symbol": sym, "ivRank": iv_rank, "ideas": ideas, "disclaimer": "教育用途，不构成投资建议。"}


@router.get("/{symbol}/earnings")
def stock_earnings(
    symbol: str,
    _: Optional[str] = Depends(bearer_subscription_optional),
    limit: int = Query(default=8, ge=1, le=24),
) -> dict[str, object]:
    sym = symbol.strip().upper()
    cfg = get_settings()
    next_dt = None
    try:
        t = yf.Ticker(sym)
        eds = getattr(t, "earnings_dates", None)
        if eds is not None and hasattr(eds, "index") and len(eds.index) > 0:
            ts0 = eds.index[0]
            next_dt = ts0.date().isoformat() if hasattr(ts0, "date") else str(ts0)
    except Exception:
        pass

    history_tuples, hist_note = build_earnings_history(
        symbol=sym,
        fmp_api_key=cfg.fmp_api_key,
        limit=limit,
    )
    moves = [
        float(e.price_window_move_pct)
        for e in history_tuples
        if e.price_window_move_pct is not None
    ]
    avg_abs_move = (
        round(sum(abs(m) for m in moves) / len(moves), 4) if moves else None
    )

    return {
        "symbol": sym,
        "nextEarningsDate": next_dt,
        "history": [
            {
                "date": e.date,
                "eps": e.eps,
                "epsEstimated": e.eps_estimated,
                "revenue": e.revenue,
                "priceWindowMovePct": e.price_window_move_pct,
                "ivCrushPct": e.iv_crush_pct,
                "source": e.source,
            }
            for e in history_tuples
        ],
        "summary": {
            "avgAbsPriceWindowMovePct": avg_abs_move,
            "eventCount": len(history_tuples),
        },
        "note": hist_note,
    }
