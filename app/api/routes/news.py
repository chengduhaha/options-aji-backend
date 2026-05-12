"""News API routes."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Query
from sqlalchemy import select, desc

from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.db.models import StockNewsRow
from app.db.session import SessionLocal
from app.services.cache_service import TTL_WARM, cache_get, cache_set, key_stock_news

router = APIRouter(prefix="/api/news", tags=["news"])


@router.get("/latest")
def get_latest_news(page: int = Query(0), limit: int = Query(20, le=100)):
    session = SessionLocal()
    try:
        rows = session.execute(
            select(StockNewsRow)
            .order_by(desc(StockNewsRow.published_at))
            .offset(page * limit)
            .limit(limit)
        ).scalars().all()
        if rows:
            return {"articles": [_row_to_dict(r) for r in rows]}
    finally:
        session.close()

    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"articles": []}
    articles = get_fmp_client().get_stock_news(page=page, limit=limit)
    return {"articles": articles}


@router.get("/stock")
def get_stock_news(
    tickers: str = Query(""),
    page: int = Query(0),
    limit: int = Query(20, le=100),
):
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if not ticker_list:
        return {"articles": []}

    # Check cache for single-ticker
    if len(ticker_list) == 1:
        cached = cache_get(key_stock_news(ticker_list[0]))
        if cached:
            return cached

    session = SessionLocal()
    try:
        from sqlalchemy import or_, func
        # JSON contains search (PostgreSQL / SQLite)
        q = select(StockNewsRow).order_by(desc(StockNewsRow.published_at)).limit(limit)
        rows = session.execute(q).scalars().all()
        filtered = [
            r for r in rows
            if any(t in (r.symbols or []) for t in ticker_list)
        ]
        if filtered:
            result = {"articles": [_row_to_dict(r) for r in filtered[:limit]]}
            if len(ticker_list) == 1:
                cache_set(key_stock_news(ticker_list[0]), result, ttl=TTL_WARM)
            return result
    finally:
        session.close()

    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"articles": []}
    articles = get_fmp_client().get_stock_news(tickers=ticker_list, page=page, limit=limit)
    result = {"articles": articles}
    if len(ticker_list) == 1:
        cache_set(key_stock_news(ticker_list[0]), result, ttl=TTL_WARM)
    return result


@router.get("/search")
def search_news(q: str = Query(...), page: int = Query(0)):
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"articles": []}
    articles = get_fmp_client().search_news(q, page=page)
    return {"articles": articles, "query": q}


def _row_to_dict(r: StockNewsRow) -> dict:
    return {
        "id": r.id,
        "symbols": r.symbols,
        "title": r.title,
        "content": r.content,
        "url": r.url,
        "source": r.source,
        "published_at": r.published_at.isoformat() if r.published_at else None,
        "title_zh": r.title_zh,
        "summary_zh": r.summary_zh,
    }
