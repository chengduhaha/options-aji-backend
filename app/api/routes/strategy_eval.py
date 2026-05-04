"""Multi-leg strategy evaluation via Black–Scholes + expiry P/L scan."""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.analytics.options_pricing import StrategyLegIn, evaluate_multi_leg
from app.api.deps import bearer_subscription_optional

router = APIRouter(prefix="/api/strategy", tags=["strategy"])

LegSide = Literal["buy", "sell"]
LegType = Literal["call", "put"]


class StrategyLeg(BaseModel):
    side: LegSide
    option_type: LegType
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


@router.post("/evaluate")
def evaluate_strategy(
    body: StrategyEvaluateRequest,
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return {
        "symbol": sym,
        **out,
    }
