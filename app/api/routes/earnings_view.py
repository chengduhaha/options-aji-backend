"""Earnings calendar API — grid-friendly grouped by date."""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import bearer_subscription_optional
from app.db.models import EarningsCalendarRow
from app.db.session import db_session_dep

router = APIRouter(prefix="/api/earnings", tags=["earnings"])


@router.get("/calendar-view")
def earnings_calendar_view(
    date_from: Optional[str] = Query(default=None, alias="from"),
    date_to: Optional[str] = Query(default=None, alias="to"),
    db: Session = Depends(db_session_dep),
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, Any]:
    """Return `{ days: { \"YYYY-MM-DD\": [ {symbol, ...}, ... ] } }`."""
    today = dt.date.today()
    if date_from:
        d0 = dt.date.fromisoformat(date_from[:10])
    else:
        d0 = today - dt.timedelta(days=today.weekday())
    if date_to:
        d1 = dt.date.fromisoformat(date_to[:10])
    else:
        d1 = d0 + dt.timedelta(days=20)

    rows = db.execute(
        select(EarningsCalendarRow)
        .where(
            EarningsCalendarRow.earnings_date >= d0,
            EarningsCalendarRow.earnings_date <= d1,
        )
        .order_by(EarningsCalendarRow.earnings_date, EarningsCalendarRow.symbol)
    ).scalars().all()

    days: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        ed = r.earnings_date
        dk = ed.isoformat() if isinstance(ed, dt.date) else str(ed)[:10]
        days.setdefault(dk, []).append(
            {
                "symbol": r.symbol,
                "epsEstimate": r.eps_estimate,
                "epsActual": r.eps_actual,
                "revenueEstimate": r.revenue_estimate,
                "revenueActual": r.revenue_actual,
                "surprisePct": r.surprise_pct,
                "time": r.time,
                "isConfirmed": r.is_confirmed,
            }
        )
    total = sum(len(v) for v in days.values())
    return {
        "from": d0.isoformat(),
        "to": d1.isoformat(),
        "days": days,
        "total": total,
    }
