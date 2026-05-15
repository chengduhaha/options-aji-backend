"""2.0 earnings endpoint under /api/earnings/{symbol}."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.analytics.earnings_depth import build_earnings_history
from app.api.deps import bearer_subscription_optional
from app.config import get_settings

router = APIRouter(prefix="/api/earnings", tags=["earnings"])


@router.get("/{symbol}")
def earnings_by_symbol(
    symbol: str,
    limit: int = Query(default=8, ge=1, le=24),
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    sym = symbol.strip().upper()
    if not sym:
        return {"error": "empty_symbol"}

    cfg = get_settings()
    history_tuples, hist_note = build_earnings_history(
        symbol=sym,
        fmp_api_key=cfg.fmp_api_key,
        limit=limit,
    )
    moves = [
        float(e.price_window_move_pct)
        for e in history_tuples
        if e.price_window_move_pct is not None
    ]
    avg_abs_move = (
        round(sum(abs(m) for m in moves) / len(moves), 4) if moves else None
    )

    return {
        "symbol": sym,
        "history": [
            {
                "date": e.date,
                "eps": e.eps,
                "epsEstimated": e.eps_estimated,
                "revenue": e.revenue,
                "priceWindowMovePct": e.price_window_move_pct,
                "ivCrushPct": e.iv_crush_pct,
                "source": e.source,
            }
            for e in history_tuples
        ],
        "summary": {
            "avgAbsPriceWindowMovePct": avg_abs_move,
            "eventCount": len(history_tuples),
        },
        "note": hist_note,
    }

