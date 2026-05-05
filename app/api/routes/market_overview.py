"""Market overview API — sectors, movers, indices, market hours."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter

from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.services.cache_service import (
    TTL_HOT, cache_get, cache_set,
    key_market_sectors, key_market_gainers, key_market_losers,
    key_market_actives, key_market_open,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/sectors")
def get_sectors():
    cached = cache_get(key_market_sectors())
    if cached:
        return cached
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"sectors": [], "pe": []}
    client = get_fmp_client()
    sectors = client.get_sector_performance()
    pe = client.get_sector_pe()
    result = {"sectors": sectors, "pe": pe, "synced_at": datetime.now(timezone.utc).isoformat()}
    cache_set(key_market_sectors(), result, ttl=TTL_HOT)
    return result


@router.get("/gainers")
def get_gainers():
    cached = cache_get(key_market_gainers())
    if cached:
        return cached
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"data": []}
    data = get_fmp_client().get_gainers()
    result = {"data": data, "synced_at": datetime.now(timezone.utc).isoformat()}
    cache_set(key_market_gainers(), result, ttl=300)
    return result


@router.get("/losers")
def get_losers():
    cached = cache_get(key_market_losers())
    if cached:
        return cached
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"data": []}
    data = get_fmp_client().get_losers()
    result = {"data": data, "synced_at": datetime.now(timezone.utc).isoformat()}
    cache_set(key_market_losers(), result, ttl=300)
    return result


@router.get("/actives")
def get_actives():
    cached = cache_get(key_market_actives())
    if cached:
        return cached
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"data": []}
    data = get_fmp_client().get_most_actives()
    result = {"data": data, "synced_at": datetime.now(timezone.utc).isoformat()}
    cache_set(key_market_actives(), result, ttl=300)
    return result


@router.get("/hours")
def get_market_hours():
    cached = cache_get(key_market_open())
    if cached:
        return cached
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"isTheStockMarketOpen": None}
    data = get_fmp_client().get_market_hours() or {}
    cache_set(key_market_open(), data, ttl=60)
    return data


@router.get("/indices")
def get_indices():
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"indices": []}
    data = get_fmp_client().get_all_index_quotes()
    return {"indices": data, "synced_at": datetime.now(timezone.utc).isoformat()}
