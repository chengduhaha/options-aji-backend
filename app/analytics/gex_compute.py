"""Local Gamma Exposure (GEX) estimate from yfinance option chains."""

from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass
from typing import Any, Optional

import yfinance as yf

logger = logging.getLogger(__name__)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_gamma(*, spot: float, strike: float, t_years: float, iv: float, rate: float = 0.052) -> float:
    if spot <= 0 or strike <= 0 or t_years <= 1e-6 or iv <= 1e-6:
        return 0.0
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))
    return _norm_pdf(d1) / (spot * iv * math.sqrt(t_years))


def _years_to_expiry(expiry_date: dt.date) -> float:
    today = dt.datetime.now(dt.timezone.utc).date()
    days = max((expiry_date - today).days, 0)
    return float(days) / 365.0


def _scalar_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        return None if math.isnan(x) else x
    if hasattr(v, "item"):
        try:
            return _scalar_float(v.item())
        except Exception:
            return None
    try:
        return _scalar_float(float(v))
    except (TypeError, ValueError):
        return None


def _scalar_int(v: Any) -> int:
    f = _scalar_float(v)
    if f is None:
        return 0
    return int(f)


@dataclass(frozen=True)
class GexStrikeRow:
    strike: float
    call_gex_bn: float
    put_gex_bn: float
    net_bn: float
    gamma: float
    oi: int
    iv: float


def compute_gex_profile(symbol: str, *, max_strikes: int = 45) -> dict[str, object]:
    """Return keys aligned with frontend `GexProfile` (camelCase added in router if needed)."""

    guard = symbol.strip().upper()
    if not guard:
        return {"symbol": "", "error": "empty_symbol"}

    try:
        t = yf.Ticker(guard)
        opts = list(t.options or [])
        qi = t.fast_info
        spot_raw = qi.get("last_price")
        spot = float(spot_raw) if isinstance(spot_raw, (int, float)) and not (isinstance(spot_raw, float) and math.isnan(spot_raw)) else 0.0
    except Exception as exc:
        logger.warning("compute_gex_profile meta(%s): %s", guard, exc)
        return {"symbol": guard, "error": "ticker_failed"}

    if not opts:
        return {"symbol": guard, "error": "no_option_chain"}

    expiry_str = opts[0]
    try:
        exp_parts = [int(x) for x in expiry_str.split("-")]
        exp_date = dt.date(exp_parts[0], exp_parts[1], exp_parts[2])
    except Exception:
        exp_date = dt.datetime.now(dt.timezone.utc).date()

    t_years = _years_to_expiry(exp_date)
    if t_years <= 0:
        t_years = 1 / 365.0

    try:
        chain = t.option_chain(expiry_str)
        calls = chain.calls
        puts = chain.puts
    except Exception as exc:
        logger.warning("compute_gex_profile chain(%s): %s", guard, exc)
        return {"symbol": guard, "error": "chain_failed"}

    if spot <= 0 and not calls.empty and "strike" in calls.columns:
        mid = float(calls["strike"].median())
        spot = mid

    strike_map: dict[float, dict[str, float]] = {}

    def ingest(df: Any, side: str) -> None:
        if df is None or df.empty:
            return
        for _, row in df.iterrows():
            k = _scalar_float(row.get("strike"))
            if k is None or k <= 0:
                continue
            oi = max(_scalar_int(row.get("openInterest")), 0)
            iv_raw = row.get("impliedVolatility")
            iv = float(iv_raw) if isinstance(iv_raw, (int, float)) and iv_raw == iv_raw and iv_raw > 0 else 0.0
            gam_raw = row.get("gamma")
            gam = _scalar_float(gam_raw)
            if gam is None or gam <= 0 or iv <= 0:
                gam = bs_gamma(spot=spot, strike=k, t_years=t_years, iv=iv)
            # Dollar GEX (billions) common scaling: gamma * OI * 100 * S^2 * 0.01 / 1e9
            mag = abs(gam) * oi * 100.0 * (spot**2) * 0.01 / 1e9
            ent = strike_map.setdefault(k, {"call": 0.0, "put": 0.0})
            if side == "call":
                ent["call"] += mag
            else:
                ent["put"] += mag

    ingest(calls, "call")
    ingest(puts, "put")

    if not strike_map:
        return {"symbol": guard, "error": "no_strikes"}

    strikes_sorted = sorted(strike_map.keys())
    # Keep ATM neighborhood
    if spot > 0:
        strikes_sorted.sort(key=lambda s: abs(s - spot))
        strikes_trimmed = sorted(strikes_sorted[:max_strikes])
    else:
        strikes_trimmed = strikes_sorted[:max_strikes]

    rows: list[GexStrikeRow] = []
    for k in sorted(strikes_trimmed):
        v = strike_map[k]
        c_mag = float(v["call"])
        p_mag = float(v["put"])
        net = c_mag - p_mag
        tot_oi = 0
        iv_atm = 0.0
        try:
            c_row = calls.loc[(calls["strike"] - k).abs().idxmin()] if not calls.empty else None
            p_row = puts.loc[(puts["strike"] - k).abs().idxmin()] if not puts.empty else None
            if c_row is not None:
                tot_oi += _scalar_int(c_row.get("openInterest"))
                iv_raw = c_row.get("impliedVolatility")
                if isinstance(iv_raw, (int, float)) and iv_raw > 0:
                    iv_atm = float(iv_raw) * 100.0
            if p_row is not None:
                tot_oi += _scalar_int(p_row.get("openInterest"))
        except Exception:
            pass
        gcomb = bs_gamma(spot=spot, strike=k, t_years=t_years, iv=(iv_atm / 100.0 if iv_atm > 0 else 0.25))
        rows.append(
            GexStrikeRow(
                strike=float(k),
                call_gex_bn=c_mag,
                put_gex_bn=p_mag,
                net_bn=net,
                gamma=float(gcomb),
                oi=int(tot_oi),
                iv=float(iv_atm),
            )
        )

    net_total = sum(r.net_bn for r in rows)
    call_wall = max(rows, key=lambda r: r.call_gex_bn).strike
    put_wall = max(rows, key=lambda r: r.put_gex_bn).strike

    # Gamma flip: cumulative net crosses zero
    cum = 0.0
    gamma_flip = float(spot)
    prev_s: Optional[float] = None
    prev_cum: Optional[float] = None
    for r in sorted(rows, key=lambda x: x.strike):
        cum += r.net_bn
        if prev_cum is not None and prev_cum != 0 and cum * prev_cum < 0 and prev_s is not None:
            # linear interp
            frac = abs(prev_cum) / (abs(prev_cum) + abs(cum))
            gamma_flip = prev_s + frac * (r.strike - prev_s)
            break
        prev_s, prev_cum = r.strike, cum

    max_pain = _max_pain_strike(calls, puts, rows)

    regime = "Positive Gamma" if net_total >= 0 else "Negative Gamma"

    strikes_out = [
        {
            "strike": round(r.strike, 2),
            "callGex": round(r.call_gex_bn, 4),
            "putGex": round(r.put_gex_bn, 4),
            "net": round(r.net_bn, 4),
            "gamma": round(r.gamma, 6),
            "oi": int(r.oi),
            "iv": round(r.iv, 4),
        }
        for r in sorted(rows, key=lambda x: x.strike)
    ]

    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    return {
        "symbol": guard,
        "expiration": str(expiry_str),
        "netGex": round(net_total, 4),
        "callWall": round(call_wall, 2),
        "putWall": round(put_wall, 2),
        "gammaFlip": round(gamma_flip, 2),
        "maxPain": round(max_pain, 2),
        "regime": regime,
        "strikes": strikes_out,
        "timestamp": ts,
        "underlyingPrice": round(spot, 2),
        "source": "yfinance_local_gamma_estimate",
    }


def _max_pain_strike(calls: Any, puts: Any, rows: list[GexStrikeRow]) -> float:
    """Minimize total intrinsic paid to longs at expiry (discrete strikes from chain)."""

    strikes_set: set[float] = set()
    call_oi: dict[float, int] = {}
    put_oi: dict[float, int] = {}
    if calls is not None and not calls.empty and "strike" in calls.columns:
        for _, row in calls.iterrows():
            k = _scalar_float(row.get("strike"))
            if k is None:
                continue
            strikes_set.add(float(k))
            call_oi[float(k)] = max(_scalar_int(row.get("openInterest")), 0)
    if puts is not None and not puts.empty and "strike" in puts.columns:
        for _, row in puts.iterrows():
            k = _scalar_float(row.get("strike"))
            if k is None:
                continue
            strikes_set.add(float(k))
            put_oi[float(k)] = max(_scalar_int(row.get("openInterest")), 0)
    if not strikes_set and rows:
        return float(rows[len(rows) // 2].strike)
    if not strikes_set:
        return 0.0

    def pain_at(price: float) -> float:
        tot = 0.0
        for k, oi in call_oi.items():
            tot += oi * 100 * max(0.0, price - k)
        for k, oi in put_oi.items():
            tot += oi * 100 * max(0.0, k - price)
        return tot

    candidates = sorted(strikes_set)
    best = candidates[0]
    best_val = pain_at(best)
    for p in candidates[1:]:
        v = pain_at(p)
        if v < best_val:
            best_val = v
            best = p
    return float(best)
