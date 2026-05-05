"""Redis cache service — hot/warm/cold TTL tiers.

Fails gracefully when Redis is not configured or unreachable.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

try:
    import redis
    _redis_available = True
except ImportError:
    _redis_available = False

from app.config import get_settings

logger = logging.getLogger(__name__)

_client: Optional[Any] = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not _redis_available:
        return None
    cfg = get_settings()
    if not cfg.redis_url:
        return None
    try:
        _client = redis.from_url(
            cfg.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            retry_on_timeout=True,
        )
        _client.ping()
        logger.info("Redis connected: %s", cfg.redis_url)
        return _client
    except Exception as exc:
        logger.warning("Redis unavailable, running without cache: %s", exc)
        _client = None
        return None


# TTL tiers (seconds)
TTL_HOT = 900    # 15 min — options snapshots, live quotes
TTL_WARM = 1800  # 30 min — news, ratings
TTL_COLD = 21600 # 6 hrs  — financials, ETF holdings
TTL_AI = 3600    # 1 hr   — AI summaries


def cache_get(key: str) -> Optional[Any]:
    """Return deserialized value or None on miss/error."""
    r = _get_client()
    if r is None:
        return None
    try:
        raw = r.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.debug("cache_get %s: %s", key, exc)
        return None


def cache_set(key: str, value: Any, ttl: int = TTL_HOT) -> None:
    """Serialize and store value with TTL. Silently ignores errors."""
    r = _get_client()
    if r is None:
        return
    try:
        r.setex(key, ttl, json.dumps(value, default=str))
    except Exception as exc:
        logger.debug("cache_set %s: %s", key, exc)


def cache_delete(key: str) -> None:
    r = _get_client()
    if r is None:
        return
    try:
        r.delete(key)
    except Exception:
        pass


def cache_delete_pattern(pattern: str) -> None:
    """Delete all keys matching a glob pattern."""
    r = _get_client()
    if r is None:
        return
    try:
        keys = r.keys(pattern)
        if keys:
            r.delete(*keys)
    except Exception as exc:
        logger.debug("cache_delete_pattern %s: %s", pattern, exc)


def is_redis_healthy() -> bool:
    r = _get_client()
    if r is None:
        return False
    try:
        return r.ping()
    except Exception:
        return False


# ── convenience key builders ────────────────────────────────────────────────────────────

def key_options_chain(symbol: str, expiry: str = "") -> str:
    return f"options:chain:{symbol.upper()}:{expiry}" if expiry else f"options:chain:{symbol.upper()}"

def key_options_snapshot(ticker: str) -> str:
    return f"options:snapshot:{ticker}"

def key_stock_quote(symbol: str) -> str:
    return f"stock:quote:{symbol.upper()}"

def key_stock_overview(symbol: str) -> str:
    return f"stock:overview:{symbol.upper()}"

def key_market_sectors() -> str:
    return "market:sectors"

def key_market_gainers() -> str:
    return "market:gainers"

def key_market_losers() -> str:
    return "market:losers"

def key_market_actives() -> str:
    return "market:actives"

def key_market_open() -> str:
    return "market:is_open"

def key_macro_calendar(date_range: str) -> str:
    return f"macro:calendar:{date_range}"

def key_treasury_rates() -> str:
    return "macro:treasury"

def key_gex(symbol: str) -> str:
    return f"gex:{symbol.upper()}"

def key_ai_market_summary() -> str:
    return "ai:market_summary"

def key_ai_stock_summary(symbol: str) -> str:
    return f"ai:stock_summary:{symbol.upper()}"

def key_congress_latest() -> str:
    return "congress:latest"

def key_insider_latest() -> str:
    return "insider:latest"

def key_analyst_ratings(symbol: str) -> str:
    return f"analyst:ratings:{symbol.upper()}"

def key_stock_news(symbol: str) -> str:
    return f"news:stock:{symbol.upper()}"

def key_etf_holdings(symbol: str) -> str:
    return f"etf:holdings:{symbol.upper()}"

def key_etf_sectors(symbol: str) -> str:
    return f"etf:sectors:{symbol.upper()}"

def key_earnings_calendar(date_range: str) -> str:
    return f"earnings:calendar:{date_range}"

def key_stock_financials(symbol: str, stmt: str) -> str:
    return f"stock:financials:{symbol.upper()}:{stmt}"
