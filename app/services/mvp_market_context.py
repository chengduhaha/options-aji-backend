"""Aggregate market context for MVP DeepAgents insights."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.api.routes.market_dashboard import market_overview
from app.api.routes.signals_feed import signals_feed
from app.db.models import TreasuryRateRow
from app.db.session import SessionLocal


def build_mvp_market_context() -> dict[str, Any]:
    overview = market_overview(_=None, refresh=False)
    signals_env = signals_feed(_=None)
    signals = signals_env.model_dump()

    treasury: dict[str, Any] = {"rates": [], "synced_at": None}
    session = SessionLocal()
    try:
        rows = session.execute(
            select(TreasuryRateRow).order_by(TreasuryRateRow.rate_date.desc()).limit(30)
        ).scalars().all()
        if rows:
            treasury["rates"] = [
                {
                    "date": str(r.rate_date),
                    "month1": r.month1,
                    "year2": r.year2,
                    "year10": r.year10,
                    "year30": r.year30,
                    "1M": r.month1,
                    "2Y": r.year2,
                    "10Y": r.year10,
                    "30Y": r.year30,
                }
                for r in rows
            ]
    finally:
        session.close()

    return {
        "overview": overview,
        "signals": signals,
        "treasury": treasury,
    }
