"""Insider trading API routes."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Query
from sqlalchemy import select, desc

from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.db.models import InsiderTradeRow
from app.db.session import SessionLocal
from app.services.cache_service import (
    TTL_WARM, cache_get, cache_set, key_insider_latest,
)

router = APIRouter(prefix="/api/insider", tags=["insider"])


@router.get("/latest")
def get_insider_latest(limit: int = Query(50, le=200)):
    cached = cache_get(key_insider_latest())
    if cached:
        return cached

    session = SessionLocal()
    try:
        rows = session.execute(
            select(InsiderTradeRow)
            .order_by(desc(InsiderTradeRow.transaction_date))
            .limit(limit)
        ).scalars().all()
        if rows:
            trades = [_row_to_dict(r) for r in rows]
            result = {"trades": trades, "synced_at": datetime.now(timezone.utc).isoformat()}
            cache_set(key_insider_latest(), result, ttl=TTL_WARM)
            return result
    finally:
        session.close()

    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"trades": []}
    trades = get_fmp_client().get_insider_trading_latest()
    result = {"trades": trades, "synced_at": datetime.now(timezone.utc).isoformat()}
    cache_set(key_insider_latest(), result, ttl=TTL_WARM)
    return result


@router.get("/{symbol}")
def get_insider_by_symbol(symbol: str, limit: int = Query(50, le=200)):
    sym = symbol.upper()
    session = SessionLocal()
    try:
        rows = session.execute(
            select(InsiderTradeRow)
            .where(InsiderTradeRow.symbol == sym)
            .order_by(desc(InsiderTradeRow.transaction_date))
            .limit(limit)
        ).scalars().all()
        if rows:
            return {"symbol": sym, "trades": [_row_to_dict(r) for r in rows]}
    finally:
        session.close()

    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"symbol": sym, "trades": []}
    trades = get_fmp_client().get_insider_trading(sym, limit=limit)
    return {"symbol": sym, "trades": trades}


def _row_to_dict(r: InsiderTradeRow) -> dict:
    return {
        "symbol": r.symbol,
        "filer_name": r.filer_name,
        "filer_relation": r.filer_relation,
        "transaction_type": r.transaction_type,
        "transaction_date": str(r.transaction_date) if r.transaction_date else None,
        "shares": r.shares,
        "price_per_share": r.price_per_share,
        "total_value": r.total_value,
        "shares_owned_after": r.shares_owned_after,
    }
