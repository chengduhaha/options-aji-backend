"""Multi-factor unusual options scoring (v2)."""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Iterable, Optional

from app.analytics.options_chain_analysis import (
    oi_prev_key,
    read_prev_oi,
    write_curr_oi,
)


def _iv_frac(row_iv: Optional[float]) -> Optional[float]:
    if row_iv is None:
        return None
    x = float(row_iv)
    if x <= 0 or math.isnan(x):
        return None
    if x > 2.5:
        return x / 100.0
    return x


def expiry_iv_stats(
    rows: list[Any],
) -> tuple[dict[str, tuple[Optional[float], float]], dict[str, float]]:
    """Per expiration ISO: (median_iv_frac, std_iv_frac), and median by key."""
    buckets: dict[str, list[float]] = {}
    for r in rows:
        exp = getattr(r, "expiration_date", None)
        if exp is None:
            continue
        ek = exp.isoformat() if isinstance(exp, dt.date) else str(exp)[:10]
        ivf = _iv_frac(getattr(r, "implied_volatility", None))
        if ivf is None:
            continue
        buckets.setdefault(ek, []).append(ivf)

    out: dict[str, tuple[Optional[float], float]] = {}
    meds: dict[str, float] = {}
    for ek, vals in buckets.items():
        s = sorted(vals)
        mid = len(s) // 2
        med = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0
        mean_v = sum(s) / len(s)
        var = sum((x - mean_v) ** 2 for x in s) / len(s)
        std = math.sqrt(var)
        out[ek] = (med, std)
        meds[ek] = med
    return out, meds


def _mid(bid: Optional[float], ask: Optional[float], midpoint: Optional[float]) -> Optional[float]:
    if midpoint is not None and midpoint > 0 and not (
        isinstance(midpoint, float) and math.isnan(midpoint)
    ):
        return float(midpoint)
    if bid is not None and ask is not None and ask > 0 and bid > 0 and ask >= bid:
        return (float(bid) + float(ask)) / 2.0
    return None


def dollar_flow_estimate(row: Any) -> float:
    bid = getattr(row, "bid", None)
    ask = getattr(row, "ask", None)
    mid = getattr(row, "midpoint", None)
    m = _mid(
        float(bid) if isinstance(bid, (int, float)) else None,
        float(ask) if isinstance(ask, (int, float)) else None,
        float(mid) if isinstance(mid, (int, float)) else None,
    )
    if m is None:
        return 0.0
    vol = int(getattr(row, "day_volume", None) or 0)
    dlt = getattr(row, "delta", None)
    if not isinstance(dlt, (int, float)) or isinstance(dlt, bool):
        ad = 0.0
    else:
        ad = abs(float(dlt))
    return m * max(vol, 0) * ad * 100.0


def score_row(
    row: Any,
    *,
    expiry_stats: dict[str, tuple[Optional[float], float]],
    update_oi_cache: bool,
) -> tuple[int, list[str], dict[str, Any]]:
    """Return score, human reasons, telemetry dict."""
    reasons: list[str] = []
    score = 0
    sym = str(getattr(row, "underlying_ticker", "") or "").upper()
    exp = getattr(row, "expiration_date", None)
    ek = exp.isoformat() if isinstance(exp, dt.date) else (str(exp)[:10] if exp else "")
    strike = float(getattr(row, "strike_price", 0) or 0)
    side = str(getattr(row, "contract_type", "") or "").lower()
    if side.startswith("c"):
        redis_side = "call"
    elif side.startswith("p"):
        redis_side = "put"
    else:
        redis_side = "unknown"

    oi = int(getattr(row, "open_interest", None) or 0)
    vol = int(getattr(row, "day_volume", None) or 0)
    oi_for_ratio = max(oi, 1)
    vol_oi = vol / float(oi_for_ratio)

    pct_oi: Optional[float] = None
    if ek and redis_side in ("call", "put"):
        key = oi_prev_key(sym, ek, strike, redis_side)
        prev = read_prev_oi(key)
        if update_oi_cache:
            write_curr_oi(key, oi)
        if prev is not None and prev > 0:
            pct_oi = (oi - prev) / float(prev)
            if pct_oi > 0.20:
                score += 30
                reasons.append(f"OI 突增 {pct_oi * 100:.0f}%")

    if vol_oi > 5:
        score += 25
        reasons.append(f"Vol/OI {vol_oi:.1f}x")
    elif vol_oi > 3:
        score += 15
        reasons.append(f"Vol/OI {vol_oi:.1f}x")

    ivf = _iv_frac(getattr(row, "implied_volatility", None))
    med_iv, std_iv = expiry_stats.get(ek, (None, 0.0))
    if ivf is not None and med_iv is not None and std_iv > 1e-8:
        dev_sigma = abs(ivf - med_iv) / std_iv
        if dev_sigma >= 2.0:
            score += 20
            reasons.append(f"IV 偏离到期中位数 {dev_sigma:.1f}σ")
        elif dev_sigma >= 1.0:
            score += 10
            reasons.append(f"IV 偏离 {dev_sigma:.1f}σ")

    flow = dollar_flow_estimate(row)
    if flow >= 1_000_000:
        score += 15
        reasons.append(f"估计资金流 ${flow / 1e6:.2f}M")
    elif flow >= 500_000:
        score += 10
        reasons.append(f"估计资金流 ${flow / 1e3:.0f}K")

    bid = getattr(row, "bid", None)
    ask = getattr(row, "ask", None)
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and ask > bid > 0:
        midp = (float(bid) + float(ask)) / 2.0
        if midp > 0:
            spr = (float(ask) - float(bid)) / midp
            if spr > 0.20:
                score += 10
                reasons.append("买卖价差偏宽")

    telem = {
        "volOiRatio": round(vol_oi, 4),
        "oiChangePct": round(pct_oi, 4) if pct_oi is not None else None,
        "estimatedFlowUsd": round(flow, 2),
    }
    return score, reasons, telem


def score_snapshot_rows(
    rows: Iterable[Any],
    *,
    min_score: int = 60,
    update_oi_cache: bool = True,
) -> list[dict[str, Any]]:
    row_list = list(rows)
    stats, _ = expiry_iv_stats(row_list)
    out: list[dict[str, Any]] = []
    for r in row_list:
        sc, reasons, telem = score_row(r, expiry_stats=stats, update_oi_cache=update_oi_cache)
        if sc < min_score:
            continue
        exp = getattr(r, "expiration_date", None)
        exp_s = exp.isoformat() if isinstance(exp, dt.date) else str(exp or "")
        direction = (
            "call"
            if str(getattr(r, "contract_type", "")).lower().startswith("c")
            else "put"
        )
        out.append(
            {
                "score": sc,
                "reasons": reasons,
                **telem,
                "ticker": getattr(r, "ticker", None),
                "underlying": getattr(r, "underlying_ticker", None),
                "contract_type": direction,
                "expiration_date": exp_s,
                "strike_price": getattr(r, "strike_price", None),
                "volume": getattr(r, "day_volume", None),
                "open_interest": getattr(r, "open_interest", None),
                "implied_volatility": getattr(r, "implied_volatility", None),
                "delta": getattr(r, "delta", None),
                "bid": getattr(r, "bid", None),
                "ask": getattr(r, "ask", None),
            }
        )
    return out
