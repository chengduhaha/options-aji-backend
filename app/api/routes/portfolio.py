"""Portfolio Greeks calculator — compute total exposure from positions."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import db_session_dep
from app.db.models import OptionsSnapshotRow

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


class Position(BaseModel):
    ticker: str = Field(..., description="Options ticker like O:SPY260515C00550000")
    quantity: int = Field(..., ge=-9999, le=9999, description="Positive=long, negative=short")


class GreeksResponse(BaseModel):
    total_delta: float = 0
    total_gamma: float = 0
    total_theta: float = 0
    total_vega: float = 0
    positions: list[dict] = []
    errors: list[str] = []


@router.post("/greeks", response_model=GreeksResponse)
def compute_portfolio_greeks(
    body: list[Position],
    db: Session = Depends(db_session_dep),
) -> GreeksResponse:
    """Calculate total portfolio Greeks from a list of option positions."""
    total_delta = 0.0
    total_gamma = 0.0
    total_theta = 0.0
    total_vega = 0.0
    positions_out: list[dict] = []
    errors: list[str] = []

    for pos in body:
        try:
            row = db.execute(
                select(OptionsSnapshotRow).where(OptionsSnapshotRow.ticker == pos.ticker)
            ).scalar_one_or_none()

            if not row:
                errors.append(f"未找到合约: {pos.ticker}")
                continue

            q = pos.quantity
            mult = 100.0  # shares per contract

            delta = (row.delta or 0) * q * mult
            gamma = (row.gamma or 0) * q * mult
            theta = (row.theta or 0) * q * mult
            vega = (row.vega or 0) * q * mult

            total_delta += delta
            total_gamma += gamma
            total_theta += theta
            total_vega += vega

            positions_out.append({
                "ticker": pos.ticker,
                "quantity": q,
                "underlying": row.underlying_ticker,
                "type": row.contract_type,
                "strike": row.strike_price,
                "expiration": str(row.expiration_date) if row.expiration_date else None,
                "delta_exposure": round(delta, 2),
                "gamma_exposure": round(gamma, 4),
                "theta_exposure": round(theta, 2),
                "vega_exposure": round(vega, 2),
                "contract_delta": row.delta,
                "contract_gamma": row.gamma,
                "contract_theta": row.theta,
                "contract_vega": row.vega,
            })
        except Exception as exc:
            errors.append(f"处理 {pos.ticker}: {exc}")

    return GreeksResponse(
        total_delta=round(total_delta, 2),
        total_gamma=round(total_gamma, 4),
        total_theta=round(total_theta, 2),
        total_vega=round(total_vega, 2),
        positions=positions_out,
        errors=errors,
    )