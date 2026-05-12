"""European Black–Scholes option pricing, Greeks, and multi-leg expiry P/L scans."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]
OptKind = Literal["call", "put"]


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(*, s: float, k: float, t: float, r: float, sigma: float) -> tuple[float, float]:
    if s <= 0 or k <= 0 or t <= 0 or sigma <= 0:
        raise ValueError("bs_inputs_invalid")
    vt = sigma * math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / vt
    d2 = d1 - vt
    return d1, d2


def bs_price(*, is_call: bool, s: float, k: float, t: float, r: float, sigma: float) -> float:
    d1, d2 = _d1_d2(s=s, k=k, t=t, r=r, sigma=sigma)
    disc = math.exp(-r * t)
    if is_call:
        return float(s * norm_cdf(d1) - k * disc * norm_cdf(d2))
    return float(k * disc * norm_cdf(-d2) - s * norm_cdf(-d1))


@dataclass(frozen=True)
class GreeksScaled:
    delta: float
    gamma: float
    vega: float
    theta: float


def bs_greeks(
    *,
    is_call: bool,
    s: float,
    k: float,
    t: float,
    r: float,
    sigma: float,
) -> GreeksScaled:
    """Greeks scaled to position size of one option contract (×100 shares)."""

    d1, d2 = _d1_d2(s=s, k=k, t=t, r=r, sigma=sigma)
    pdf_d1 = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    gamma_per_share = pdf_d1 / (s * sigma * math.sqrt(t))
    mult = 100.0

    if is_call:
        delta_per = norm_cdf(d1)
        theta_per = (
            -(s * pdf_d1 * sigma) / (2.0 * math.sqrt(t))
            - r * k * math.exp(-r * t) * norm_cdf(d2)
        ) / 365.0
    else:
        delta_per = norm_cdf(d1) - 1.0
        theta_per = (
            -(s * pdf_d1 * sigma) / (2.0 * math.sqrt(t))
            + r * k * math.exp(-r * t) * norm_cdf(-d2)
        ) / 365.0

    vega_per = (s * pdf_d1 * math.sqrt(t)) / 100.0
    return GreeksScaled(
        delta=float(delta_per * mult),
        gamma=float(gamma_per_share * mult),
        vega=float(vega_per * mult),
        theta=float(theta_per * mult),
    )


@dataclass(frozen=True)
class StrategyLegIn:
    side: Side
    option_type: OptKind
    strike: float
    premium: float
    contracts: int
    days_to_expiry: float
    iv: float


def _intrinsic(*, is_call: bool, spot: float, strike: float) -> float:
    if is_call:
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


def leg_pnl_dollar_at_expiry(leg: StrategyLegIn, spot: float) -> float:
    intrinsic = _intrinsic(
        is_call=leg.option_type == "call",
        spot=spot,
        strike=leg.strike,
    )
    mult = leg.contracts * 100
    if leg.side == "buy":
        return mult * (intrinsic - leg.premium)
    return mult * (leg.premium - intrinsic)


def net_pnl_expiry(legs: list[StrategyLegIn], spot: float) -> float:
    return sum(leg_pnl_dollar_at_expiry(l, spot) for l in legs)


def net_greeks_contracts(legs: list[StrategyLegIn], spot: float, r: float) -> dict[str, float]:
    d = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    for leg in legs:
        t = max(1e-8, float(leg.days_to_expiry) / 365.0)
        g = bs_greeks(
            is_call=leg.option_type == "call",
            s=spot,
            k=leg.strike,
            t=t,
            r=r,
            sigma=leg.iv,
        )
        w = leg.contracts * (1.0 if leg.side == "buy" else -1.0)
        d["delta"] += g.delta * w
        d["gamma"] += g.gamma * w
        d["vega"] += g.vega * w
        d["theta"] += g.theta * w
    return d


def evaluate_multi_leg(
    *,
    spot: float,
    risk_free: float,
    legs: list[StrategyLegIn],
    spot_moves_pct: list[float],
) -> dict[str, object]:
    if spot <= 0 or not legs:
        raise ValueError("evaluate_inputs_invalid")

    curve: list[dict[str, object]] = []
    for pct in spot_moves_pct:
        s = spot * (1.0 + float(pct) / 100.0)
        curve.append(
            {
                "spotMovePct": float(pct),
                "spot": round(s, 4),
                "pnlAtExpiry": round(net_pnl_expiry(legs, s), 2),
            },
        )

    dense_count = 400
    lo = spot * 0.55
    hi = spot * 1.45
    step = (hi - lo) / float(dense_count - 1) if dense_count > 1 else 1.0
    dense_pnls = [net_pnl_expiry(legs, lo + i * step) for i in range(dense_count)]

    max_profit = max(dense_pnls)
    max_loss = min(dense_pnls)

    breakevens: list[float] = []
    for i in range(len(dense_pnls) - 1):
        a, b = dense_pnls[i], dense_pnls[i + 1]
        if a == 0:
            breakevens.append(round(lo + i * step, 4))
        elif (a < 0 < b) or (b < 0 < a):
            x0 = lo + i * step
            x1 = x0 + step
            t_frac = -a / (b - a) if b != a else 0.0
            breakevens.append(round(x0 + t_frac * step, 4))

    net_flow = sum(
        leg.premium * leg.contracts * 100 * (1.0 if leg.side == "sell" else -1.0)
        for leg in legs
    )

    return {
        "pnlBySpotPct": curve,
        "maxProfitScan": round(max_profit, 2),
        "maxLossScan": round(max_loss, 2),
        "breakevensApprox": breakevens[:6],
        "netPremiumFlow": round(net_flow, 2),
        "greeksAtSpot": net_greeks_contracts(legs, spot, risk_free),
        "disclaimer": "到期 P/L 为欧式内在价值；未计入佣金与提前平仓。",
    }
