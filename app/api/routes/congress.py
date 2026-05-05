"""Congress trading API routes."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Query
from sqlalchemy import select, desc

from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.db.models import CongressTradeRow
from app.db.session import SessionLocal
from app.services.cache_service import (
    TTL_WARM, cache_get, cache_set, key_congress_latest,
)

router = APIRouter(prefix="/api/congress", tags=["congress"])


@router.get("/latest")
def get_congress_latest(
    chamber: str = Query(""),  # senate / house / ""
    limit: int = Query(50, le=200),
):
    cached = cache_get(key_congress_latest())
    if cached and not chamber:
        return cached

    session = SessionLocal()
    try:
        q = select(CongressTradeRow).order_by(desc(CongressTradeRow.transaction_date)).limit(limit)
        if chamber:
            q = q.where(CongressTradeRow.chamber == chamber)
        rows = session.execute(q).scalars().all()
        if rows:
            trades = [_row_to_dict(r) for r in rows]
            result = {"trades": trades, "synced_at": datetime.now(timezone.utc).isoformat()}
            if not chamber:
                cache_set(key_congress_latest(), result, ttl=TTL_WARM)
            return result
    finally:
        session.close()

    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"trades": []}
    client = get_fmp_client()
    senate = client.get_senate_latest_trading()
    house = client.get_house_latest_trading()
    combined = senate + house if not chamber else (senate if chamber == "senate" else house)
    result = {"trades": combined, "synced_at": datetime.now(timezone.utc).isoformat()}
    if not chamber:
        cache_set(key_congress_latest(), result, ttl=TTL_WARM)
    return result


@router.get("/{symbol}")
def get_congress_by_symbol(symbol: str, limit: int = Query(50, le=200)):
    sym = symbol.upper()
    session = SessionLocal()
    try:
        rows = session.execute(
            select(CongressTradeRow)
            .where(CongressTradeRow.symbol == sym)
            .order_by(desc(CongressTradeRow.transaction_date))
            .limit(limit)
        ).scalars().all()
        if rows:
            return {"symbol": sym, "trades": [_row_to_dict(r) for r in rows]}
    finally:
        session.close()

    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"symbol": sym, "trades": []}
    client = get_fmp_client()
    senate = client.get_senate_trading_by_symbol(sym)
    house = client.get_house_trading_by_symbol(sym)
    return {"symbol": sym, "trades": senate + house}


def _row_to_dict(r: CongressTradeRow) -> dict:
    return {
        "chamber": r.chamber,
        "member_name": r.member_name,
        "symbol": r.symbol,
        "asset_description": r.asset_description,
        "transaction_type": r.transaction_type,
        "transaction_date": str(r.transaction_date) if r.transaction_date else None,
        "amount_range": r.amount_range,
        "filing_date": str(r.filing_date) if r.filing_date else None,
    }
