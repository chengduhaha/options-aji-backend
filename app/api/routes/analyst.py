"""Analyst ratings API routes."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Query
from sqlalchemy import select, desc

from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.db.models import AnalystRatingRow
from app.db.session import SessionLocal
from app.services.cache_service import TTL_WARM, cache_get, cache_set, key_analyst_ratings

router = APIRouter(prefix="/api/analyst", tags=["analyst"])


@router.get("/{symbol}")
def get_analyst_ratings(symbol: str, limit: int = Query(30, le=100)):
    sym = symbol.upper()
    cached = cache_get(key_analyst_ratings(sym))
    if cached:
        return cached

    session = SessionLocal()
    try:
        rows = session.execute(
            select(AnalystRatingRow)
            .where(AnalystRatingRow.symbol == sym)
            .order_by(desc(AnalystRatingRow.rating_date))
            .limit(limit)
        ).scalars().all()
        if rows:
            result = {
                "symbol": sym,
                "ratings": [
                    {
                        "analyst_company": r.analyst_company,
                        "action": r.rating_action,
                        "from": r.rating_from,
                        "to": r.rating_to,
                        "price_target": r.price_target,
                        "date": str(r.rating_date) if r.rating_date else None,
                    }
                    for r in rows
                ],
                "synced_at": datetime.now(timezone.utc).isoformat(),
            }
            cache_set(key_analyst_ratings(sym), result, ttl=TTL_WARM)
            return result
    finally:
        session.close()

    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"symbol": sym, "ratings": []}
    client = get_fmp_client()
    ratings = client.get_analyst_ratings(sym)[:limit]
    pt_summary = client.get_price_target_summary(sym)
    pt_consensus = client.get_price_target_consensus(sym)
    result = {
        "symbol": sym,
        "ratings": ratings,
        "price_target_summary": pt_summary,
        "price_target_consensus": pt_consensus,
    }
    cache_set(key_analyst_ratings(sym), result, ttl=TTL_WARM)
    return result


@router.get("/{symbol}/price-target")
def get_price_target(symbol: str):
    sym = symbol.upper()
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"symbol": sym}
    client = get_fmp_client()
    return {
        "symbol": sym,
        "summary": client.get_price_target_summary(sym),
        "consensus": client.get_price_target_consensus(sym),
    }
