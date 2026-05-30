"""Local Gamma Exposure (GEX) estimate from yfinance option chains."""

from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass
from typing import Any, Optional

from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.tools.yf_helpers import yf_ticker

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


def _quote_spot_from_fmp(symbol: str) -> Optional[float]:
    if not get_settings().fmp_api_key.strip():
        return None
    try:
        row = get_fmp_client().get_quote(symbol)
    except Exception as exc:
        logger.warning("compute_gex_profile fmp_quote(%s): %s", symbol, exc)
        return None
    if not isinstance(row, dict):
        return None
    for key in ("price", "last_price", "regularMarketPrice", "currentPrice"):
        value = _scalar_float(row.get(key))
        if value is not None and value > 0:
            return value
    return None


def _resolve_spot(symbol: str, yf_spot: float) -> tuple[float, str]:
    quote_spot = _quote_spot_from_fmp(symbol)
    if quote_spot is not None and quote_spot > 0:
        return quote_spot, "fmp_quote"
    if yf_spot > 0:
        return yf_spot, "yfinance_fast_info"
    return 0.0, "missing"


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
        t = yf_ticker(guard)
        opts = list(t.options or [])
        qi = t.fast_info
        spot_raw = qi.get("last_price")
        yf_spot = float(spot_raw) if isinstance(spot_raw, (int, float)) and not (isinstance(spot_raw, float) and math.isnan(spot_raw)) else 0.0
        spot, spot_source = _resolve_spot(guard, yf_spot)
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
        spot_source = "strike_median_fallback"

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
        "spotSource": spot_source,
        "source": "yfinance_local_gamma_estimate",
    }


def compute_gex_profile_from_contracts(
    symbol: str,
    *,
    contracts: list[dict[str, Any]],
    spot: float,
    max_strikes: int = 45,
) -> dict[str, object]:
    """Estimate GEX from already-fetched realtime option contracts."""

    guard = symbol.strip().upper()
    if not guard:
        return {"symbol": "", "error": "empty_symbol"}
    if spot <= 0:
        return {"symbol": guard, "error": "missing_spot"}
    if not contracts:
        return {"symbol": guard, "error": "no_option_chain"}

    strike_map: dict[float, dict[str, float]] = {}
    oi_map: dict[float, int] = {}
    iv_map: dict[float, float] = {}
    expiry: Optional[str] = None

    for row in contracts:
        side = str(row.get("contract_type") or "").lower()
        if side not in ("call", "put"):
            continue
        strike = _scalar_float(row.get("strike_price"))
        if strike is None or strike <= 0:
            continue
        oi = max(_scalar_int(row.get("open_interest")), 0)
        gamma = _scalar_float(row.get("gamma"))
        iv = _scalar_float(row.get("implied_volatility")) or 0.0
        if gamma is None or gamma <= 0:
            exp_raw = row.get("expiration_date")
            t_years = 30 / 365.0
            if isinstance(exp_raw, str) and len(exp_raw) >= 10:
                try:
                    exp_date = dt.date.fromisoformat(exp_raw[:10])
                    t_years = max(_years_to_expiry(exp_date), 1 / 365.0)
                except ValueError:
                    pass
            gamma = bs_gamma(spot=spot, strike=strike, t_years=t_years, iv=iv if iv > 0 else 0.35)
        mag = abs(gamma) * oi * 100.0 * (spot**2) * 0.01 / 1e9
        ent = strike_map.setdefault(strike, {"call": 0.0, "put": 0.0})
        ent[side] += mag
        oi_map[strike] = oi_map.get(strike, 0) + oi
        if iv > 0:
            iv_map[strike] = max(iv_map.get(strike, 0.0), iv * 100.0)
        if expiry is None and row.get("expiration_date"):
            expiry = str(row.get("expiration_date"))

    if not strike_map:
        return {"symbol": guard, "error": "no_strikes"}

    strikes_sorted = sorted(strike_map.keys(), key=lambda s: abs(s - spot))
    strikes_trimmed = sorted(strikes_sorted[:max_strikes])
    rows = [
        GexStrikeRow(
            strike=float(k),
            call_gex_bn=float(strike_map[k]["call"]),
            put_gex_bn=float(strike_map[k]["put"]),
            net_bn=float(strike_map[k]["call"] - strike_map[k]["put"]),
            gamma=bs_gamma(spot=spot, strike=k, t_years=30 / 365.0, iv=(iv_map.get(k, 35.0) / 100.0)),
            oi=int(oi_map.get(k, 0)),
            iv=float(iv_map.get(k, 0.0)),
        )
        for k in strikes_trimmed
    ]

    net_total = sum(r.net_bn for r in rows)
    call_wall = max(rows, key=lambda r: r.call_gex_bn).strike
    put_wall = max(rows, key=lambda r: r.put_gex_bn).strike
    gamma_flip = _gamma_flip_from_rows(rows, spot=spot)
    max_pain = _max_pain_from_gex_rows(rows)
    regime = "Positive Gamma" if net_total >= 0 else "Negative Gamma"
    ts = dt.datetime.now(dt.timezone.utc).isoformat()

    return {
        "symbol": guard,
        "expiration": expiry or "",
        "netGex": round(net_total, 4),
        "callWall": round(call_wall, 2),
        "putWall": round(put_wall, 2),
        "gammaFlip": round(gamma_flip, 2),
        "maxPain": round(max_pain, 2),
        "regime": regime,
        "strikes": [
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
        ],
        "timestamp": ts,
        "underlyingPrice": round(spot, 2),
        "spotSource": "futu_quote",
        "source": "futu_realtime_gamma_estimate",
    }


def _gamma_flip_from_rows(rows: list[GexStrikeRow], *, spot: float) -> float:
    cum = 0.0
    gamma_flip = float(spot)
    prev_s: Optional[float] = None
    prev_cum: Optional[float] = None
    for r in sorted(rows, key=lambda x: x.strike):
        cum += r.net_bn
        if prev_cum is not None and prev_cum != 0 and cum * prev_cum < 0 and prev_s is not None:
            frac = abs(prev_cum) / (abs(prev_cum) + abs(cum))
            gamma_flip = prev_s + frac * (r.strike - prev_s)
            break
        prev_s, prev_cum = r.strike, cum
    return gamma_flip


def _max_pain_from_gex_rows(rows: list[GexStrikeRow]) -> float:
    if not rows:
        return 0.0
    return max(rows, key=lambda r: r.call_gex_bn + r.put_gex_bn).strike


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
