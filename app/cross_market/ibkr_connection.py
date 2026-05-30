"""Singleton IB API connection for Interactive Brokers Gateway / TWS."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ib_insync import IB

logger = logging.getLogger(__name__)

_ib: IB | None = None
_connect_lock: asyncio.Lock | None = None


def _connect_lock_get() -> asyncio.Lock:
    global _connect_lock
    if _connect_lock is None:
        _connect_lock = asyncio.Lock()
    return _connect_lock


def ibkr_is_enabled() -> bool:
    return os.getenv("IBKR_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def get_ib() -> IB:
    """Return shared IB instance (connect via ibkr_ensure_connected before API calls)."""
    global _ib
    if _ib is None:
        from ib_insync import IB

        _ib = IB()
        _ib.RequestTimeout = float(os.getenv("IBKR_REQUEST_TIMEOUT", "25"))
    return _ib


def ibkr_is_connected() -> bool:
    if _ib is None:
        return False
    return bool(_ib.isConnected())


def ibkr_managed_accounts() -> list[str]:
    """Account ids for this login (lightweight; no balances or positions)."""
    if not ibkr_is_enabled() or not ibkr_is_connected():
        return []
    ib = get_ib()
    try:
        accts = ib.managedAccounts()
        if accts is None:
            return []
        if isinstance(accts, str):
            return [p.strip() for p in accts.split(",") if p.strip()]
        return [str(a).strip() for a in accts if str(a).strip()]
    except Exception:
        logger.exception("IBKR managedAccounts failed")
        return []


async def ibkr_ensure_connected() -> bool:
    """Connect lazily on the current event loop (avoids uvicorn lifespan / ib_insync loop mismatch)."""
    global _ib
    if not ibkr_is_enabled():
        return False
    ib = get_ib()
    if ib.isConnected():
        return True
    host = os.getenv("IBKR_HOST", "127.0.0.1").strip()
    port = int(os.getenv("IBKR_PORT", "4002").strip())
    client_id = int(os.getenv("IBKR_CLIENT_ID", "1").strip())
    ro = os.getenv("IBKR_API_READONLY", "").strip().lower() in ("1", "true", "yes", "on")
    lock = _connect_lock_get()
    async with lock:
        ib = get_ib()
        if ib.isConnected():
            return True
        try:
            await asyncio.wait_for(
                ib.connectAsync(host, port, clientId=client_id, timeout=20, readonly=ro),
                timeout=25.0,
            )
            logger.info("IBKR connected %s:%s clientId=%s", host, port, client_id)
            return True
        except Exception:
            logger.exception("IBKR connect failed host=%s port=%s", host, port)
            try:
                if _ib is not None and _ib.isConnected():
                    _ib.disconnect()
            except Exception:
                pass
            _ib = None
            return False


async def ibkr_connect_startup() -> bool:
    """Deprecated name: use ibkr_ensure_connected from request handlers."""
    return await ibkr_ensure_connected()


async def ibkr_disconnect_shutdown() -> None:
    if _ib is None:
        return
    try:
        if _ib.isConnected():
            _ib.disconnect()
            logger.info("IBKR disconnected")
    except Exception:
        logger.exception("IBKR disconnect error")


async def ibkr_req_current_time_async() -> float | None:
    """Return UNIX epoch seconds from IB if connected; else None."""
    if not ibkr_is_enabled():
        return None
    await ibkr_ensure_connected()
    if not ibkr_is_connected():
        return None
    ib = get_ib()
    try:
        dt = await ib.reqCurrentTimeAsync()
        return float(dt.timestamp())
    except Exception:
        logger.exception("IBKR reqCurrentTime failed")
        return None
