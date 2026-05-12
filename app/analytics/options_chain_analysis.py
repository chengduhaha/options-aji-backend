"""Options chain anomaly metrics: parity, OI deltas, IV skew outliers, liquidity."""
from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass
from typing import Any, Optional

from app.services.cache_service import redis_client_optional

logger = logging.getLogger(__name__)

RISK_FREE_DEFAULT = 0.0438  # ~10Y Treasury proxy
OI_KEY_TTL_SEC = 86400 * 2


def oi_prev_key(symbol: str, exp_iso: str, strike: float, side: str) -> str:
    return f"opts:oi_prev:{symbol.upper()}:{exp_iso}:{strike:.6f}:{side.lower()}"


def read_prev_oi(key: str) -> Optional[int]:
    r = redis_client_optional()
    if r is None:
        return None
    try:
        raw = r.get(key)
        if raw is None:
            return None
        return int(raw)
    except Exception as exc:
        logger.debug("read_prev_oi %s: %s", key, exc)
        return None


def write_curr_oi(key: str, oi: int) -> None:
    r = redis_client_optional()
    if r is None:
        return
    try:
        r.setex(key, OI_KEY_TTL_SEC, str(int(oi)))
    except Exception as exc:
        logger.debug("write_curr_oi %s: %s", key, exc)


def _mid_price(
    bid: Optional[float],
    ask: Optional[float],
    midpoint: Optional[float],
) -> Optional[float]:
    if midpoint is not None and midpoint > 0 and not (isinstance(midpoint, float) and math.isnan(midpoint)):
        return float(midpoint)
    if (
        bid is not None
        and ask is not None
        and bid > 0
        and ask > 0
        and ask >= bid
    ):
        return (float(bid) + float(ask)) / 2.0
    return None


def _spread_ratio(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None or ask is None or ask <= bid:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid


def _years_to_expiry(expiry: dt.date, as_of: dt.date) -> float:
    days = max((expiry - as_of).days, 0)
    return float(days) / 365.0


@dataclass
class StrikePair:
    strike: float
    call: Optional[Any]
    put: Optional[Any]


def _iv_frac(row_iv: Optional[float]) -> Optional[float]:
    """Store IV as fractional (Massive/OpenBB mixes); normalized to decimal for stats."""
    if row_iv is None:
        return None
    x = float(row_iv)
    if x <= 0 or math.isnan(x):
        return None
    if x > 2.5:  # already percent-like 25..200
        return x / 100.0
    return x


def build_chain_analysis(
    *,
    symbol: str,
    expiration_iso: str,
    rows_call: list[Any],
    rows_put: list[Any],
    as_of_date: dt.date | None = None,
    risk_free_rate: float = RISK_FREE_DEFAULT,
) -> dict[str, Any]:
    """Merge call/put rows by strike and attach analysis markers."""
    as_of = as_of_date or dt.datetime.now(dt.timezone.utc).date()
    try:
        exp_d = dt.date.fromisoformat(expiration_iso[:10])
    except ValueError:
        exp_d = as_of

    by_strike: dict[float, StrikePair] = {}
    spot: Optional[float] = None

    def ingest(rows: list[Any], side: str) -> None:
        nonlocal spot
        for r in rows:
            k = getattr(r, "strike_price", None)
            if k is None:
                continue
            strike_f = float(k)
            entry = by_strike.setdefault(strike_f, StrikePair(strike=strike_f, call=None, put=None))
            if side == "call":
                entry.call = r
            else:
                entry.put = r
            up = getattr(r, "underlying_price", None)
            if isinstance(up, (int, float)) and up > 0 and not (isinstance(up, float) and math.isnan(up)):
                spot = float(up)

    ingest(rows_call, "call")
    ingest(rows_put, "put")

    iv_pool: list[float] = []
    for sp in by_strike.values():
        for r in (sp.call, sp.put):
            if r is None:
                continue
            ivf = _iv_frac(getattr(r, "implied_volatility", None))
            if ivf is None:
                continue
            iv_pool.append(ivf)

    median_iv: Optional[float] = None
    std_iv = 0.0
    if iv_pool:
        sorted_iv = sorted(iv_pool)
        mid = len(sorted_iv) // 2
        if len(sorted_iv) % 2:
            median_iv = sorted_iv[mid]
        else:
            median_iv = (sorted_iv[mid - 1] + sorted_iv[mid]) / 2.0
        mean_iv = sum(sorted_iv) / len(sorted_iv)
        variance = sum((x - mean_iv) ** 2 for x in sorted_iv) / len(sorted_iv)
        std_iv = math.sqrt(variance)

    t_years = _years_to_expiry(exp_d, as_of)
    discount = math.exp(-risk_free_rate * t_years) if t_years > 0 else 1.0

    strikes_out: list[dict[str, Any]] = []
    highlights: list[dict[str, Any]] = []

    for strike in sorted(by_strike):
        pair = by_strike[strike]
        call_r, put_r = pair.call, pair.put

        c_mid = _mid_price(
            getattr(call_r, "bid", None) if call_r else None,
            getattr(call_r, "ask", None) if call_r else None,
            getattr(call_r, "midpoint", None) if call_r else None,
        )
        p_mid = _mid_price(
            getattr(put_r, "bid", None) if put_r else None,
            getattr(put_r, "ask", None) if put_r else None,
            getattr(put_r, "midpoint", None) if put_r else None,
        )

        parity_dev_dollar: Optional[float] = None
        parity_dev_pct: Optional[float] = None
        parity_flag = False
        if (
            spot
            and spot > 0
            and c_mid is not None
            and p_mid is not None
            and t_years >= 0
        ):
            lhs = c_mid - p_mid
            rhs = spot - strike * discount
            parity_dev_dollar = abs(lhs - rhs)
            parity_dev_pct = parity_dev_dollar / spot
            if parity_dev_pct > 0.005 or (parity_dev_dollar is not None and parity_dev_dollar > 0.5):
                parity_flag = True

        call_oi_chg: Optional[float] = None
        put_oi_chg: Optional[float] = None
        call_oi_spike = False
        put_oi_spike = False

        for side_label, row, spike_attr in (
            ("call", call_r, "call_oi_spike"),
            ("put", put_r, "put_oi_spike"),
        ):
            if row is None:
                continue
            oi = int(getattr(row, "open_interest", None) or 0)
            kredis = oi_prev_key(symbol, expiration_iso[:10], float(strike), side_label)
            prev = read_prev_oi(kredis)
            write_curr_oi(kredis, oi)
            if prev is None or prev <= 0:
                pct_chg = None
            else:
                pct_chg = (oi - prev) / float(prev)
            if side_label == "call":
                call_oi_chg = pct_chg
                if pct_chg is not None and pct_chg > 0.20:
                    call_oi_spike = True
            else:
                put_oi_chg = pct_chg
                if pct_chg is not None and pct_chg > 0.20:
                    put_oi_spike = True

        iv_z_call: Optional[float] = None
        iv_skew_call = False
        if call_r is not None and median_iv:
            cf = _iv_frac(getattr(call_r, "implied_volatility", None))
            if cf and std_iv > 1e-6:
                iv_z_call = abs(cf - median_iv) / std_iv
                iv_skew_call = iv_z_call >= 2.0

        iv_z_put: Optional[float] = None
        iv_skew_put = False
        if put_r is not None and median_iv:
            pf = _iv_frac(getattr(put_r, "implied_volatility", None))
            if pf and std_iv > 1e-6:
                iv_z_put = abs(pf - median_iv) / std_iv
                iv_skew_put = iv_z_put >= 2.0

        def liq_label(r: Any) -> str:
            if r is None:
                return "unknown"
            sr = _spread_ratio(getattr(r, "bid", None), getattr(r, "ask", None))
            if sr is None:
                return "unknown"
            if sr < 0.05:
                return "high"
            if sr > 0.20:
                return "low"
            return "normal"

        call_liq = liq_label(call_r)
        put_liq = liq_label(put_r)

        row_obj: dict[str, Any] = {
            "strike": strike,
            "parity": {
                "deviationDollar": round(parity_dev_dollar, 4) if parity_dev_dollar is not None else None,
                "deviationPct": round(parity_dev_pct, 6) if parity_dev_pct is not None else None,
                "flag": parity_flag,
            },
            "call": _serialize_leg(call_r, {
                "oiChangePct": round(call_oi_chg, 4) if call_oi_chg is not None else None,
                "oiSpike": call_oi_spike,
                "ivZ": round(iv_z_call, 3) if iv_z_call is not None else None,
                "ivSkewOutlier": iv_skew_call,
                "liquidity": call_liq,
                "spreadRatio": (lambda sr: round(sr, 6) if sr is not None else None)(
                    _spread_ratio(getattr(call_r, "bid", None), getattr(call_r, "ask", None)),
                )
                if call_r
                else None,
            }),
            "put": _serialize_leg(put_r, {
                "oiChangePct": round(put_oi_chg, 4) if put_oi_chg is not None else None,
                "oiSpike": put_oi_spike,
                "ivZ": round(iv_z_put, 3) if iv_z_put is not None else None,
                "ivSkewOutlier": iv_skew_put,
                "liquidity": put_liq,
                "spreadRatio": (lambda sr: round(sr, 6) if sr is not None else None)(
                    _spread_ratio(getattr(put_r, "bid", None), getattr(put_r, "ask", None)),
                )
                if put_r
                else None,
            }),
        }
        strikes_out.append(row_obj)

        if parity_flag:
            highlights.append({
                "type": "parity",
                "strike": strike,
                "side": "pair",
                "detail": f"Call-Put 平价偏离 ≈ ${parity_dev_dollar:.2f}" if parity_dev_dollar else "平价偏离",
            })
        if call_oi_spike:
            highlights.append({
                "type": "oi_spike",
                "strike": strike,
                "side": "call",
                "detail": f"Call OI 变化 +{(call_oi_chg or 0) * 100:.0f}%",
            })
        if put_oi_spike:
            highlights.append({
                "type": "oi_spike",
                "strike": strike,
                "side": "put",
                "detail": f"Put OI 变化 +{(put_oi_chg or 0) * 100:.0f}%",
            })
        if iv_skew_call:
            highlights.append({
                "type": "iv_skew",
                "strike": strike,
                "side": "call",
                "detail": f"IV 偏离中位数 {iv_z_call:.1f}σ",
            })
        if iv_skew_put:
            highlights.append({
                "type": "iv_skew",
                "strike": strike,
                "side": "put",
                "detail": f"IV 偏离中位数 {iv_z_put:.1f}σ",
            })

    total_call_oi = sum(
        int(getattr(sp.call, "open_interest", None) or 0) for sp in by_strike.values() if sp.call
    )
    total_put_oi = sum(
        int(getattr(sp.put, "open_interest", None) or 0) for sp in by_strike.values() if sp.put
    )
    pcr_oi = (total_put_oi / total_call_oi) if total_call_oi > 0 else None

    return {
        "symbol": symbol.upper(),
        "expiration": expiration_iso[:10],
        "underlyingPrice": spot,
        "riskFreeRate": risk_free_rate,
        "ivMedian": round(median_iv * 100, 4) if median_iv else None,
        "ivStdPercent": round(std_iv * 100, 4) if std_iv else None,
        "summary": {
            "totalCallOi": total_call_oi,
            "totalPutOi": total_put_oi,
            "putCallOiRatio": round(pcr_oi, 4) if pcr_oi is not None else None,
        },
        "highlights": highlights[:40],
        "strikes": strikes_out,
    }


def _serialize_leg(row: Any, extra: dict[str, Any]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    iv = getattr(row, "implied_volatility", None)
    iv_pct: Optional[float] = None
    if isinstance(iv, (int, float)) and iv == iv:
        iv_pct = float(iv) * 100.0 if float(iv) <= 2.5 else float(iv)
    return {
        "ticker": getattr(row, "ticker", None),
        "bid": getattr(row, "bid", None),
        "ask": getattr(row, "ask", None),
        "midpoint": getattr(row, "midpoint", None),
        "openInterest": getattr(row, "open_interest", None),
        "dayVolume": getattr(row, "day_volume", None),
        "impliedVolatilityPct": round(iv_pct, 4) if iv_pct is not None else None,
        "delta": getattr(row, "delta", None),
        "gamma": getattr(row, "gamma", None),
        "theta": getattr(row, "theta", None),
        "vega": getattr(row, "vega", None),
        **extra,
    }


def analysis_from_yfinance_rows(
    *,
    symbol: str,
    expiration_iso: str,
    calls: list[dict[str, Any]],
    puts: list[dict[str, Any]],
    underlying_price: Optional[float],
) -> dict[str, Any]:
    """Wrap dict rows as simple namespace objects for build_chain_analysis."""

    class _R:
        __slots__ = (
            "ticker", "strike_price", "bid", "ask", "midpoint", "open_interest",
            "day_volume", "implied_volatility", "delta", "gamma", "theta", "vega",
            "underlying_price",
        )

        def __init__(self, d: dict[str, Any], typ: str) -> None:
            self.ticker = str(d.get("contractSymbol") or d.get("ticker") or f"{typ}")
            self.strike_price = float(d.get("strike") or 0)
            b, a = d.get("bid"), d.get("ask")
            self.bid = float(b) if isinstance(b, (int, float)) else None
            self.ask = float(a) if isinstance(a, (int, float)) else None
            last = d.get("lastPrice")
            self.midpoint = float(last) if isinstance(last, (int, float)) else None
            oi = d.get("openInterest")
            self.open_interest = int(oi) if isinstance(oi, (int, float)) else 0
            vol = d.get("volume")
            self.day_volume = int(vol) if isinstance(vol, (int, float)) else 0
            iv = d.get("impliedVolatility")
            self.implied_volatility = float(iv) if isinstance(iv, (int, float)) else None
            for g in ("delta", "gamma", "theta", "vega"):
                v = d.get(g)
                setattr(self, g, float(v) if isinstance(v, (int, float)) else None)
            self.underlying_price = underlying_price

    c_rows = [_R(x, "call") for x in calls if isinstance(x, dict)]
    p_rows = [_R(x, "put") for x in puts if isinstance(x, dict)]
    return build_chain_analysis(
        symbol=symbol,
        expiration_iso=expiration_iso,
        rows_call=c_rows,
        rows_put=p_rows,
    )
