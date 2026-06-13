"""S&P 500 constituent symbols for options sync (FMP)."""
from __future__ import annotations

import logging

from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.services.cache_service import TTL_COLD, cache_get, cache_set

logger = logging.getLogger(__name__)

_CACHE_KEY = "sync:sp500:symbols:v1"
_CURSOR_KEY = "sync:sp500:cursor:v1"


def load_sp500_symbols() -> list[str]:
    """Return sorted unique S&P 500 tickers; falls back to watchlist when FMP unavailable."""
    if cached := cache_get(_CACHE_KEY):
        if isinstance(cached, list):
            return [str(s).upper() for s in cached if s]

    cfg = get_settings()
    symbols: list[str] = []
    if cfg.fmp_api_key:
        try:
            rows = get_fmp_client().get_sp500_components()
            for row in rows:
                sym = str(row.get("symbol") or "").strip().upper()
                if sym:
                    symbols.append(sym)
        except Exception as exc:
            logger.warning("load_sp500_symbols fmp failed: %s", exc)

    if not symbols:
        symbols = list(cfg.sync_watchlist_symbols)

    deduped = sorted(set(symbols))
    cache_set(_CACHE_KEY, deduped, ttl=TTL_COLD)
    return deduped


def next_sp500_batch(batch_size: int) -> list[str]:
    """Round-robin batch of S&P 500 symbols for incremental options sync."""
    all_symbols = load_sp500_symbols()
    if not all_symbols:
        return []
    size = max(1, min(batch_size, len(all_symbols)))
    cursor = 0
    if raw := cache_get(_CURSOR_KEY):
        try:
            cursor = int(raw) % len(all_symbols)
        except (TypeError, ValueError):
            cursor = 0
    batch = [all_symbols[(cursor + i) % len(all_symbols)] for i in range(size)]
    cache_set(_CURSOR_KEY, (cursor + size) % len(all_symbols), ttl=TTL_COLD)
    return batch
