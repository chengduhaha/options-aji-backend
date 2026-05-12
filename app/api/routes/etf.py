"""ETF API routes."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.services.cache_service import (
    TTL_COLD, cache_get, cache_set, key_etf_holdings, key_etf_sectors,
)

router = APIRouter(prefix="/api/etf", tags=["etf"])


@router.get("/list")
def get_etf_list():
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"etfs": []}
    data = get_fmp_client().get_etf_list()
    return {"etfs": data}


@router.get("/{symbol}/holdings")
def get_etf_holdings(symbol: str):
    sym = symbol.upper()
    cached = cache_get(key_etf_holdings(sym))
    if cached:
        return cached
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"symbol": sym, "holdings": []}
    holdings = get_fmp_client().get_etf_holdings(sym)
    result = {"symbol": sym, "holdings": holdings, "synced_at": datetime.now(timezone.utc).isoformat()}
    cache_set(key_etf_holdings(sym), result, ttl=TTL_COLD)
    return result


@router.get("/{symbol}/info")
def get_etf_info(symbol: str):
    sym = symbol.upper()
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"symbol": sym}
    info = get_fmp_client().get_etf_info(sym)
    return {"symbol": sym, "info": info}


@router.get("/{symbol}/sectors")
def get_etf_sectors(symbol: str):
    sym = symbol.upper()
    cached = cache_get(key_etf_sectors(sym))
    if cached:
        return cached
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"symbol": sym, "sectors": []}
    sectors = get_fmp_client().get_etf_sector_weighting(sym)
    countries = get_fmp_client().get_etf_country_allocation(sym)
    result = {
        "symbol": sym,
        "sectors": sectors,
        "countries": countries,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set(key_etf_sectors(sym), result, ttl=TTL_COLD)
    return result
