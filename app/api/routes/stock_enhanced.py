"""Enhanced stock detail routes — financials, DCF, analyst, search."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Query
from sqlalchemy import select, desc

from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.db.models import EarningsCalendarRow, StockQuoteRow
from app.db.session import SessionLocal
from app.services.cache_service import (
    TTL_COLD, TTL_WARM, TTL_HOT, cache_get, cache_set,
    key_stock_quote, key_stock_financials, key_earnings_calendar,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/stock", tags=["stock_enhanced"])


@router.get("/search")
def search_stock(q: str = Query(...), limit: int = Query(10, le=30)):
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"results": []}
    client = get_fmp_client()
    by_symbol = client.search_symbol(q, limit=limit)
    by_name = client.search_name(q, limit=limit)
    seen = set()
    combined = []
    for item in by_symbol + by_name:
        sym = item.get("symbol", "")
        if sym and sym not in seen:
            seen.add(sym)
            combined.append(item)
    return {"results": combined[:limit]}


@router.get("/{symbol}/quote")
def get_stock_quote(symbol: str):
    sym = symbol.upper()
    cached = cache_get(key_stock_quote(sym))
    if cached:
        return cached

    session = SessionLocal()
    try:
        row = session.get(StockQuoteRow, sym)
        if row:
            result = {
                "symbol": sym,
                "price": row.price, "change": row.change, "change_pct": row.change_pct,
                "day_high": row.day_high, "day_low": row.day_low,
                "year_high": row.year_high, "year_low": row.year_low,
                "volume": row.volume, "avg_volume": row.avg_volume,
                "market_cap": row.market_cap, "pe": row.pe, "eps": row.eps,
                "open": row.open_price, "previous_close": row.previous_close,
                "snapshot_time": row.snapshot_time.isoformat() if row.snapshot_time else None,
            }
            cache_set(key_stock_quote(sym), result, ttl=TTL_HOT)
            return result
    finally:
        session.close()

    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"symbol": sym, "error": "no_data"}
    data = get_fmp_client().get_quote(sym)
    if data:
        cache_set(key_stock_quote(sym), data, ttl=TTL_HOT)
    return data or {"symbol": sym, "error": "quote_fetch_failed"}


@router.get("/{symbol}/financials")
def get_stock_financials(
    symbol: str,
    statement: str = Query("income"),  # income / balance / cashflow
    period: str = Query("quarter"),
    limit: int = Query(8, le=20),
):
    sym = symbol.upper()
    cache_key = key_stock_financials(sym, f"{statement}_{period}")
    cached = cache_get(cache_key)
    if cached:
        return cached

    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"symbol": sym, "statement": statement, "data": []}
    client = get_fmp_client()

    if statement == "income":
        data = client.get_income_statement(sym, period, limit)
    elif statement == "balance":
        data = client.get_balance_sheet(sym, period, limit)
    elif statement == "cashflow":
        data = client.get_cash_flow(sym, period, limit)
    else:
        return {"symbol": sym, "error": "invalid statement type"}

    result = {"symbol": sym, "statement": statement, "period": period, "data": data}
    cache_set(cache_key, result, ttl=TTL_COLD)
    return result


@router.get("/{symbol}/metrics")
def get_stock_metrics(symbol: str, period: str = Query("quarter")):
    sym = symbol.upper()
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"symbol": sym}
    client = get_fmp_client()
    metrics = client.get_key_metrics(sym, period)
    metrics_ttm = client.get_key_metrics_ttm(sym)
    ratios_ttm = client.get_financial_ratios_ttm(sym)
    scores = client.get_financial_scores(sym)
    return {
        "symbol": sym,
        "metrics": metrics,
        "metrics_ttm": metrics_ttm,
        "ratios_ttm": ratios_ttm,
        "scores": scores,
    }


@router.get("/{symbol}/dcf")
def get_stock_dcf(symbol: str):
    sym = symbol.upper()
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"symbol": sym}
    return get_fmp_client().get_dcf(sym) or {"symbol": sym, "error": "no_data"}


@router.get("/{symbol}/earnings-calendar")
def get_earnings_calendar(symbol: str, limit: int = Query(8, le=20)):
    sym = symbol.upper()
    session = SessionLocal()
    try:
        rows = session.execute(
            select(EarningsCalendarRow)
            .where(EarningsCalendarRow.symbol == sym)
            .order_by(desc(EarningsCalendarRow.earnings_date))
            .limit(limit)
        ).scalars().all()
        if rows:
            return {
                "symbol": sym,
                "earnings": [
                    {
                        "date": str(r.earnings_date),
                        "eps_estimate": r.eps_estimate,
                        "eps_actual": r.eps_actual,
                        "surprise_pct": r.surprise_pct,
                        "time": r.time,
                        "is_confirmed": r.is_confirmed,
                    }
                    for r in rows
                ],
            }
    finally:
        session.close()

    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"symbol": sym, "earnings": []}
    # Fallback: live FMP /earnings (not surprises — that endpoint returns empty)
    try:
        raw = get_fmp_client().get_earnings_history(sym)
        parsed = [
            {
                "date": str(r.get("date", "")),
                "eps_estimate": r.get("epsEstimated"),
                "eps_actual": r.get("epsActual"),
                "surprise_pct": r.get("surprisePct"),
                "time": r.get("time"),
                "is_confirmed": bool(r.get("updatedFromDate")),
            }
            for r in raw[:limit]
        ]
        return {"symbol": sym, "earnings": parsed, "source": "live_api"}
    except Exception as exc:
        logger.warning("Earnings live fallback failed for %s: %s", sym, exc)
    return {"symbol": sym, "earnings": []}


@router.get("/{symbol}/profile")
def get_company_profile(symbol: str):
    sym = symbol.upper()
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"symbol": sym}
    client = get_fmp_client()
    profile = client.get_profile(sym)
    peers = client.get_peers(sym)
    executives = client.get_executives(sym)
    return {
        "symbol": sym,
        "profile": profile,
        "peers": peers,
        "executives": executives,
    }


@router.get("/{symbol}/history")
def get_stock_history(
    symbol: str,
    from_date: str = Query(""),
    to_date: str = Query(""),
    interval: str = Query("daily"),  # daily / 5min / 1hour
):
    sym = symbol.upper()
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"symbol": sym, "bars": []}
    client = get_fmp_client()
    if interval == "daily":
        bars = client.get_historical_price_eod(sym, from_date=from_date, to_date=to_date)
    else:
        bars = client.get_intraday_chart(sym, interval=interval, from_date=from_date, to_date=to_date)
    return {"symbol": sym, "interval": interval, "bars": bars}
