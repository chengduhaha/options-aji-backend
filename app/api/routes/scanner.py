"""Lightweight options scanner over liquidity watchlist."""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import bearer_subscription_optional
from app.api.routes.market_dashboard import WATCHLIST_MOVER
from app.tools.openbb_tools import build_default_toolkit

router = APIRouter(prefix="/api/scanner", tags=["scanner"])


class ScanRequest(BaseModel):
    preset: str = Field(
        default="high_vol_oi",
        description="high_vol_oi | high_iv_rank | low_iv_rank | otp",
    )
    min_volume: int = Field(default=300, ge=0)
    vol_oi_ratio: float = Field(default=3.0, ge=0)
    iv_rank_min: float = Field(default=60.0, ge=0, le=100)
    iv_rank_max: float = Field(default=40.0, ge=0, le=100)


class ScanRow(BaseModel):
    symbol: str
    option_type: str
    strike: float
    expiration: str
    volume: float
    openInterest: float
    volOiRatio: float
    ivRankProxy: Optional[int] = None
    delta: Optional[float] = None


class ScanResponse(BaseModel):
    preset: str
    generated_at_unix: float
    duration_ms: float
    count: int
    results: list[ScanRow]


@router.post("/run", response_model=ScanResponse)
def scanner_run(
    body: ScanRequest,
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> ScanResponse:
    t0 = time.perf_counter()
    tk = build_default_toolkit()
    rows: list[ScanRow] = []

    for sym in WATCHLIST_MOVER:
        bar = tk.frontend_market_bar(sym)
        iv_rank = bar.get("ivRank")
        rank_f = float(iv_rank) if isinstance(iv_rank, (int, float)) else None

        if body.preset == "high_iv_rank" and (rank_f is None or rank_f < body.iv_rank_min):
            continue
        if body.preset == "low_iv_rank" and (rank_f is None or rank_f > body.iv_rank_max):
            continue

        ch = tk.get_option_chain_full(sym)
        if not isinstance(ch, dict) or ch.get("error"):
            continue
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
                strike = rec.get("strike")
                if vol < body.min_volume or oi < 1:
                    continue
                ratio = vol / max(oi, 1.0)
                if body.preset in ("high_vol_oi", "high_iv_rank", "low_iv_rank") and ratio < body.vol_oi_ratio:
                    continue
                dlt = rec.get("delta")
                delta_f = float(dlt) if isinstance(dlt, (int, float)) else None
                if body.preset == "otp" and (
                    delta_f is None
                    or abs(delta_f) > 0.35
                    or vol < 800
                ):
                    continue
                rows.append(
                    ScanRow(
                        symbol=sym,
                        option_type=side,
                        strike=float(strike) if isinstance(strike, (int, float)) else 0.0,
                        expiration=exp,
                        volume=vol,
                        openInterest=oi,
                        volOiRatio=round(ratio, 4),
                        ivRankProxy=int(rank_f) if rank_f is not None else None,
                        delta=delta_f,
                    )
                )

    rows.sort(key=lambda r: r.volOiRatio, reverse=True)
    top = rows[:120]
    dt_ms = (time.perf_counter() - t0) * 1000.0
    return ScanResponse(
        preset=body.preset,
        generated_at_unix=time.time(),
        duration_ms=round(dt_ms, 2),
        count=len(top),
        results=top,
    )
