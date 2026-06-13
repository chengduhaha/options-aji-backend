"""Aggregate market context for MVP DeepAgents insights."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.api.routes.signals_feed import get_cached_signals_feed
from app.db.models import TreasuryRateRow
from app.db.session import SessionLocal
from app.services.cache_service import cache_get, key_market_dashboard_overview


def build_mvp_market_context() -> dict[str, Any]:
    overview = cache_get(key_market_dashboard_overview())
    if not isinstance(overview, dict):
        overview = {"fromCache": False, "cacheMiss": True}
    signals_env = get_cached_signals_feed("zh")
    signals = (
        signals_env.model_dump()
        if signals_env is not None
        else {"generated_at_utc": None, "source": "signals_cache_miss", "signals": []}
    )

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
