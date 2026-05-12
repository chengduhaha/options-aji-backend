"""IV / HV proxies: yfinance lacks long ATM IV history; methodology is explicit in responses."""

from __future__ import annotations

import logging
import math
from typing import Optional

import yfinance as yf

logger = logging.getLogger(__name__)


def _log_returns(closes: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        if a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def historical_volatility(closes: list[float], trading_days: int) -> Optional[float]:
    """Annualized HV using the last `trading_days` daily log returns (needs trading_days+1 closes)."""

    if len(closes) < trading_days + 1:
        return None
    window = closes[-(trading_days + 1) :]
    rets = _log_returns(window)
    if len(rets) < 10:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
    if var <= 0:
        return None
    return float(math.sqrt(var) * math.sqrt(252)) * 100.0


def hv_series_and_current(symbol: str) -> tuple[list[tuple[str, float]], dict[str, object]]:
    """Return list of (date.iso, hv20_pct) for ~1y and metadata."""

    guard = symbol.strip().upper()
    if not guard:
        return [], {"error": "empty_symbol"}

    try:
        t = yf.Ticker(guard)
        hist = t.history(period="1y", interval="1d", auto_adjust=True)
    except Exception as exc:
        logger.warning("hv_series(%s): %s", guard, exc)
        return [], {"symbol": guard, "error": "history_failed"}

    if hist is None or hist.empty or "Close" not in hist.columns:
        return [], {"symbol": guard, "error": "no_history"}

    closes = [float(x) for x in hist["Close"].tolist() if x == x]
    idx = hist.index

    series: list[tuple[str, float]] = []
    for i in range(20, len(closes)):
        hv = historical_volatility(closes[: i + 1], 20)
        if hv is None:
            continue
        try:
            ts = idx[i]
            if hasattr(ts, "date"):
                d = ts.date().isoformat()
            else:
                d = str(ts)[:10]
        except Exception:
            d = ""
        series.append((d, hv))

    hv20 = historical_volatility(closes, 20)
    hv60 = historical_volatility(closes, 60)

    meta: dict[str, object] = {
        "symbol": guard,
        "hv20": None if hv20 is None else round(hv20, 4),
        "hv60": None if hv60 is None else round(hv60, 4),
        "methodology": "HV20/HV60 from log-returns, annualized * sqrt(252) as percent.",
    }
    return series[-260:], meta


def iv_rank_percentile_proxy(
    *,
    current_iv_pct: float,
    hv_series_pct: list[float],
) -> tuple[Optional[float], Optional[float], str]:
    """Rank current_iv vs historical HV distribution (proxy for IV rank).

    Returns (iv_rank_0_100, iv_percentile_0_100, methodology_note).
    """

    clean = [v for v in hv_series_pct if v == v and v > 0]
    if not clean or current_iv_pct <= 0:
        return None, None, "insufficient_hv_history"

    lo, hi = min(clean), max(clean)
    if hi <= lo:
        return 50.0, 50.0, "iv_rank_vs_hv_flat_distribution"

    rank = (current_iv_pct - lo) / (hi - lo) * 100.0
    rank = max(0.0, min(100.0, rank))
    below = sum(1 for v in clean if v < current_iv_pct)
    pct = below / len(clean) * 100.0
    note = "iv_rank_and_percentile_vs_1y_hv20_samples_proxy_not_atm_iv_history"
    return round(rank, 2), round(pct, 2), note


def vix_term_structure_hint() -> dict[str, object]:
    """Near vs next month VIX (proxy for futures): use ^VIX and VIX3M if available."""

    out: dict[str, object] = {"label": "unavailable", "structure": None}
    try:
        vx = yf.Ticker("^VIX")
        v3 = yf.Ticker("^VIX3M")
        q1 = vx.fast_info.get("last_price")
        q2 = v3.fast_info.get("last_price")
        if isinstance(q1, (int, float)) and isinstance(q2, (int, float)) and q1 > 0 and q2 > 0:
            near, far = float(q1), float(q2)
            structure = "contango" if far > near else "backwardation"
            out = {
                "near_month_proxy": round(near, 2),
                "far_month_proxy": round(far, 2),
                "structure": structure,
                "label": "VIX_vs_VIX3M_proxy",
            }
    except Exception as exc:
        logger.warning("vix_term_structure_hint: %s", exc)
    return out
