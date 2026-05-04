"""Placeholder multi-leg strategy evaluation (extend with full BS engine)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import bearer_subscription_optional

router = APIRouter(prefix="/api/strategy", tags=["strategy"])


class StrategyLeg(BaseModel):
    side: str = Field(description="buy | sell")
    option_type: str = Field(description="call | put")
    strike: float
    premium: float
    contracts: int = 1


class StrategyEvaluateRequest(BaseModel):
    symbol: str
    spot: float
    legs: list[StrategyLeg]


@router.post("/evaluate")
def evaluate_strategy(
    body: StrategyEvaluateRequest,
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    max_pay = sum(l.premium * l.contracts * 100 * (1 if l.side == "sell" else -1) for l in body.legs)
    return {
        "symbol": body.symbol.upper(),
        "spot": body.spot,
        "net_credit_debit_preview": round(max_pay, 2),
        "note": "MVP 占位：后续接入 Black-Scholes 与 PoP 估算。",
    }
