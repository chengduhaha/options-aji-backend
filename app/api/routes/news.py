"""News API routes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    max_age_hours: int = Query(24, ge=1, le=168),
):
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if not ticker_list:
        return {"articles": []}

    # Check cache for single-ticker
    if len(ticker_list) == 1:
        cached = _normalize_stock_news_cache(cache_get(key_stock_news(ticker_list[0])))
        if cached and _is_fresh_payload(cached, max_age_hours):
            return cached

    session = SessionLocal()
    try:
        # JSON contains search (PostgreSQL / SQLite)
        q = select(StockNewsRow).order_by(desc(StockNewsRow.published_at)).limit(limit)
        rows = session.execute(q).scalars().all()
        filtered = [
            r for r in rows
            if any(t in (r.symbols or []) for t in ticker_list)
        ]
        if filtered:
            fresh_rows = [
                r for r in filtered
                if r.published_at and _is_recent_dt(r.published_at, max_age_hours)
            ]
            if fresh_rows:
                result = _freshness_payload([_row_to_dict(r) for r in fresh_rows[:limit]], "db", max_age_hours)
                if len(ticker_list) == 1:
                    cache_set(key_stock_news(ticker_list[0]), result, ttl=TTL_WARM)
                return result
    finally:
        session.close()

    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"articles": []}
    articles = get_fmp_client().get_stock_news(tickers=ticker_list, page=page, limit=limit)
    result = _freshness_payload(articles, "fmp", max_age_hours)
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


def _parse_article_dt(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _article_dt(article: dict) -> datetime | None:
    for key in ("published_at", "publishedDate", "date", "datetime"):
        parsed = _parse_article_dt(article.get(key))
        if parsed:
            return parsed
    return None


def _is_recent_dt(value: datetime, max_age_hours: int) -> bool:
    dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return dt >= datetime.now(timezone.utc) - timedelta(hours=max_age_hours)


def _freshness_payload(articles: list[dict], source: str, max_age_hours: int) -> dict:
    latest = max((_article_dt(article) for article in articles if isinstance(article, dict)), default=None)
    latest_iso = latest.isoformat() if latest else None
    return {
        "articles": articles,
        "source": source,
        "freshness": {
            "latest_published_at": latest_iso,
            "max_age_hours": max_age_hours,
            "is_fresh": bool(latest and _is_recent_dt(latest, max_age_hours)),
        },
    }


def _normalize_stock_news_cache(payload: object) -> dict | None:
    """Legacy sync wrote a bare article list; API routes expect { articles, ... }."""
    if isinstance(payload, list):
        return {"articles": payload, "source": "cache"}
    if isinstance(payload, dict) and isinstance(payload.get("articles"), list):
        return payload
    return None


def _is_fresh_payload(payload: dict, max_age_hours: int) -> bool:
    articles = payload.get("articles")
    if not isinstance(articles, list):
        return False
    latest = max((_article_dt(article) for article in articles if isinstance(article, dict)), default=None)
    return bool(latest and _is_recent_dt(latest, max_age_hours))
