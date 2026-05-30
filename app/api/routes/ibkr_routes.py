"""Interactive Brokers Gateway HTTP surface (ib-insync)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.cross_market import ibkr_connection as ibkr_conn
from app.cross_market.ibkr_client import (
    get_ibkr_news_headlines,
    get_option_chain_params_ibkr,
    get_option_chain_snapshot_ibkr,
    get_option_quote_ibkr,
    get_stock_quote_ibkr,
)

router = APIRouter(prefix="/api/ibkr", tags=["ibkr"])

_UNAVAILABLE = frozenset({"ibkr_disabled", "ibkr_not_connected"})


@router.get("/health")
async def ibkr_health() -> dict:
    """Whether IBKR integration is enabled, socket connected, IB server time, and managed account ids."""
    server_time = await ibkr_conn.ibkr_req_current_time_async()
    sock_ok = ibkr_conn.ibkr_is_connected() or (server_time is not None)
    managed = ibkr_conn.ibkr_managed_accounts() if sock_ok else []
    return {
        "ibkr_enabled": ibkr_conn.ibkr_is_enabled(),
        "connected": sock_ok,
        "server_time_unix": server_time,
        "managed_accounts": managed,
    }


@router.get("/quote/{symbol}")
async def ibkr_quote(symbol: str) -> dict:
    snapshot = await get_stock_quote_ibkr(symbol)
    if snapshot.get("ok"):
        return snapshot
    err = str(snapshot.get("error") or "unknown")
    status = 503 if err in _UNAVAILABLE else 502
    raise HTTPException(status_code=status, detail=snapshot)


@router.get("/options/chain-params/{symbol}")
async def ibkr_option_chain_params(symbol: str, expiry: str | None = None) -> dict:
    """SMART option expirations and strikes."""
    params: dict = await get_option_chain_params_ibkr(symbol)
    if expiry is not None and expiry.strip():
        params = {
            **params,
            "requested_expiry": expiry.strip(),
            "expiry_filter_note": "not_applied_phase_a_chain_structure_only",
        }
    if params.get("ok"):
        return params
    err = str(params.get("error") or "unknown")
    status = 503 if err in _UNAVAILABLE else 502
    raise HTTPException(status_code=status, detail=params)


@router.get("/options/quote/{symbol}")
async def ibkr_option_quote(
    symbol: str,
    expiry: str = Query(..., description="Expiry YYYYMMDD"),
    strike: float = Query(...),
    right: str = Query(..., description="C or P"),
) -> dict:
    """Single option: bid/ask/last, volume, OI, model IV & Greeks."""
    ru = right.strip().upper()[:1]
    if ru not in ("C", "P"):
        raise HTTPException(status_code=422, detail="right must be C or P")
    snap = await get_option_quote_ibkr(symbol, expiry, strike, ru)
    if snap.get("ok"):
        return snap
    err = str(snap.get("error") or "unknown")
    status = 503 if err in _UNAVAILABLE else 502
    raise HTTPException(status_code=status, detail=snap)


@router.get("/options/snapshot/{symbol}")
async def ibkr_option_chain_snapshot(
    symbol: str,
    expiry: str | None = Query(default=None, description="YYYYMMDD; default = nearest >= today"),
    strikes_each_side: int = Query(default=4, ge=1, le=12),
) -> dict:
    """ATM-focused calls+puts with IV/Greeks (bounded number of market data lines)."""
    snap = await get_option_chain_snapshot_ibkr(symbol, expiry=expiry, strikes_each_side=strikes_each_side)
    if snap.get("ok"):
        return snap
    err = str(snap.get("error") or "unknown")
    status = 503 if err in _UNAVAILABLE else 502
    raise HTTPException(status_code=status, detail=snap)


@router.get("/news/{symbol}")
async def ibkr_symbol_news(symbol: str, limit: int = Query(default=15, ge=1, le=300)) -> dict:
    """Historical news headlines for underlying (IB news subscriptions may apply)."""
    payload = await get_ibkr_news_headlines(symbol, limit=limit)
    if payload.get("ok"):
        return payload
    err = str(payload.get("error") or "unknown")
    status = 503 if err in _UNAVAILABLE else 502
    raise HTTPException(status_code=status, detail=payload)
