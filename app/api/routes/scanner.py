"""Lightweight options scanner over liquidity watchlist."""

from __future__ import annotations

import datetime as dt
import time
from typing import Literal, Optional

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
    dte_min: Optional[int] = Field(default=None, ge=0, le=3650)
    dte_max: Optional[int] = Field(default=None, ge=0, le=3650)
    delta_min: Optional[float] = Field(default=None, ge=0, le=1)
    delta_max: Optional[float] = Field(default=None, ge=0, le=1)
    iv_min: Optional[float] = Field(default=None, ge=0, le=500)
    iv_max: Optional[float] = Field(default=None, ge=0, le=500)
    expiration_scope: Literal["all", "front", "next_three"] = "next_three"
    query_text: Optional[str] = Field(default=None, max_length=300)
    symbols: Optional[list[str]] = None


class ScanRow(BaseModel):
    symbol: str
    option_type: str
    strike: float
    expiration: str
    dte: Optional[int] = None
    volume: float
    openInterest: float
    volOiRatio: float
    ivRankProxy: Optional[int] = None
    iv: Optional[float] = None
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
    if body.dte_min is not None and body.dte_max is not None and body.dte_min > body.dte_max:
        body.dte_min, body.dte_max = body.dte_max, body.dte_min
    if body.delta_min is not None and body.delta_max is not None and body.delta_min > body.delta_max:
        body.delta_min, body.delta_max = body.delta_max, body.delta_min
    if body.iv_min is not None and body.iv_max is not None and body.iv_min > body.iv_max:
        body.iv_min, body.iv_max = body.iv_max, body.iv_min

    t0 = time.perf_counter()
    tk = build_default_toolkit()
    if body.query_text:
        q = body.query_text.lower()
        if "high iv" in q or "高 iv" in q:
            body.preset = "high_iv_rank"
        elif "low iv" in q or "低 iv" in q:
            body.preset = "low_iv_rank"
        elif "near atm" in q or "近 at" in q or "otp" in q:
            body.preset = "otp"
        else:
            body.preset = "high_vol_oi"

    symbols = WATCHLIST_MOVER
    if body.symbols:
        symbols = [s.strip().upper() for s in body.symbols if s.strip()]
        if not symbols:
            symbols = WATCHLIST_MOVER

    rows: list[ScanRow] = []
    today = dt.date.today()

    for sym in symbols:
        bar = tk.frontend_market_bar(sym)
        iv_rank = bar.get("ivRank")
        rank_f = float(iv_rank) if isinstance(iv_rank, (int, float)) else None

        if body.preset == "high_iv_rank" and (rank_f is None or rank_f < body.iv_rank_min):
            continue
        if body.preset == "low_iv_rank" and (rank_f is None or rank_f > body.iv_rank_max):
            continue

        digest = tk.get_option_chain(sym, head=1)
        expirations_raw = digest.get("expirations") if isinstance(digest, dict) else None
        expirations = [str(item) for item in expirations_raw] if isinstance(expirations_raw, list) else []
        if not expirations:
            ch = tk.get_option_chain_full(sym)
            single_exp = str(ch.get("expiration") or "") if isinstance(ch, dict) else ""
            expirations = [single_exp] if single_exp else []

        if body.expiration_scope == "front":
            selected_expirations = expirations[:1]
        elif body.expiration_scope == "next_three":
            selected_expirations = expirations[:3]
        else:
            selected_expirations = expirations

        for exp in selected_expirations:
            ch = tk.get_option_chain_full(sym, expiration=exp)
            if not isinstance(ch, dict) or ch.get("error"):
                continue
            expiration = str(ch.get("expiration") or exp)
            dte: Optional[int] = None
            if len(expiration) >= 10:
                try:
                    exp_date = dt.date.fromisoformat(expiration[:10])
                    dte = (exp_date - today).days
                except ValueError:
                    dte = None
            if body.dte_min is not None and (dte is None or dte < body.dte_min):
                continue
            if body.dte_max is not None and (dte is None or dte > body.dte_max):
                continue

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
                    abs_delta = abs(delta_f) if delta_f is not None else None
                    if body.delta_min is not None and (abs_delta is None or abs_delta < body.delta_min):
                        continue
                    if body.delta_max is not None and (abs_delta is None or abs_delta > body.delta_max):
                        continue
                    if body.preset == "otp" and (
                        delta_f is None
                        or abs(delta_f) > 0.35
                        or vol < 800
                    ):
                        continue

                    iv_raw = rec.get("impliedVolatility")
                    iv_pct: Optional[float] = None
                    if isinstance(iv_raw, (int, float)):
                        iv_float = float(iv_raw)
                        if iv_float > 0:
                            iv_pct = iv_float * 100.0
                    if body.iv_min is not None and (iv_pct is None or iv_pct < body.iv_min):
                        continue
                    if body.iv_max is not None and (iv_pct is None or iv_pct > body.iv_max):
                        continue

                    rows.append(
                        ScanRow(
                            symbol=sym,
                            option_type=side,
                            strike=float(strike) if isinstance(strike, (int, float)) else 0.0,
                            expiration=expiration,
                            dte=dte,
                            volume=vol,
                            openInterest=oi,
                            volOiRatio=round(ratio, 4),
                            ivRankProxy=int(rank_f) if rank_f is not None else None,
                            iv=round(iv_pct, 2) if iv_pct is not None else None,
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
