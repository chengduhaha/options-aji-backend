"""Macro economics API — calendar, treasury rates, economic indicators."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.db.models import MacroCalendarRow, TreasuryRateRow
from app.db.session import SessionLocal
from app.services.cache_service import (
    TTL_WARM, cache_get, cache_set,
    key_macro_calendar, key_treasury_rates,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/macro", tags=["macro"])


@router.get("/calendar")
def get_macro_calendar(
    from_date: str = Query(""),
    to_date: str = Query(""),
    country: str = Query(""),
    impact: str = Query(""),
):
    today = datetime.now(timezone.utc)
    if not from_date:
        from_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    if not to_date:
        to_date = (today + timedelta(days=30)).strftime("%Y-%m-%d")

    cache_key = key_macro_calendar(f"{from_date}_{to_date}")
    cached = cache_get(cache_key)
    if cached and not country and not impact:
        return cached

    # Try DB first
    session = SessionLocal()
    try:
        q = select(MacroCalendarRow).where(
            MacroCalendarRow.event_date >= from_date,
            MacroCalendarRow.event_date <= to_date,
        ).order_by(MacroCalendarRow.event_date)
        if country:
            q = q.where(MacroCalendarRow.country == country)
        if impact:
            q = q.where(MacroCalendarRow.impact == impact)

        rows = session.execute(q).scalars().all()
        if rows:
            events = [
                {
                    "date": r.event_date.isoformat(),
                    "country": r.country,
                    "event": r.event_name,
                    "impact": r.impact,
                    "estimate": r.estimate,
                    "previous": r.previous,
                    "actual": r.actual,
                }
                for r in rows
            ]
            result = {"events": events, "from": from_date, "to": to_date}
            if not country and not impact:
                cache_set(cache_key, result, ttl=TTL_WARM)
            return result
    finally:
        session.close()

    # Fallback: live FMP
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"events": [], "from": from_date, "to": to_date}
    events = get_fmp_client().get_economic_calendar(from_date, to_date)
    result = {"events": events, "from": from_date, "to": to_date, "synced_at": today.isoformat()}
    cache_set(cache_key, result, ttl=TTL_WARM)
    return result


@router.get("/treasury")
def get_treasury_rates(days: int = Query(30, le=365)):
    cached = cache_get(key_treasury_rates())
    if cached:
        return cached

    session = SessionLocal()
    try:
        rows = session.execute(
            select(TreasuryRateRow).order_by(TreasuryRateRow.rate_date.desc()).limit(days)
        ).scalars().all()
        if rows:
            rates = [
                {
                    "date": str(r.rate_date),
                    "1M": r.month1, "2M": r.month2, "3M": r.month3, "6M": r.month6,
                    "1Y": r.year1, "2Y": r.year2, "5Y": r.year5,
                    "10Y": r.year10, "30Y": r.year30,
                }
                for r in rows
            ]
            result = {"rates": rates, "synced_at": datetime.now(timezone.utc).isoformat()}
            cache_set(key_treasury_rates(), result, ttl=TTL_WARM)
            return result
    finally:
        session.close()

    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"rates": []}
    today = datetime.now(timezone.utc)
    from_date = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    raw = get_fmp_client().get_treasury_rates(from_date=from_date)
    result = {"rates": raw, "synced_at": today.isoformat()}
    cache_set(key_treasury_rates(), result, ttl=TTL_WARM)
    return result


@router.get("/indicator")
def get_economic_indicator(name: str = Query("GDP")):
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"name": name, "data": []}
    data = get_fmp_client().get_economic_indicator(name)
    return {"name": name, "data": data}
