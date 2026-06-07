"""Daily stock history with Futu primary + yfinance fallback."""
from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from app.config import get_settings

logger = logging.getLogger(__name__)

MAX_STALE_CALENDAR_DAYS = 10


def history_last_date(hist: object) -> dt.date | None:
    if hist is None or getattr(hist, "empty", True):
        return None
    try:
        idx = hist.index[-1]
        if hasattr(idx, "date"):
            parsed = idx.date()
            if isinstance(parsed, dt.date):
                return parsed
        raw = str(idx)[:10]
        return dt.date.fromisoformat(raw)
    except (ValueError, IndexError, TypeError, AttributeError):
        return None


def is_history_fresh(hist: object, *, max_stale_days: int = MAX_STALE_CALENDAR_DAYS) -> bool:
    last = history_last_date(hist)
    if last is None:
        return False
    return (dt.date.today() - last).days <= max_stale_days


def fetch_yfinance_daily_history(symbol: str, *, period: str = "1y") -> pd.DataFrame | None:
    from app.tools.yf_helpers import yf_ticker

    sym = symbol.strip().upper()
    if not sym:
        return None
    try:
        hist = yf_ticker(sym).history(period=period, interval="1d", auto_adjust=True)
        if hist is None or getattr(hist, "empty", True):
            return None
        return hist
    except Exception as exc:
        logger.warning("yfinance history failed %s: %s", sym, exc)
        return None


def fetch_daily_stock_history(symbol: str, *, count: int = 280) -> tuple[object | None, str]:
    """Return (dataframe, source) where source is futu | yfinance | none."""

    sym = symbol.strip().upper()
    if not sym:
        return None, "none"

    cfg = get_settings()
    futu_on = getattr(cfg, "futu_enabled", False) and getattr(cfg, "futu_daily_klines_enabled", True)
    if futu_on:
        try:
            from app.clients.futu_client import get_futu_client

            hist = get_futu_client().get_daily_klines(sym, count=count)
            if is_history_fresh(hist):
                return hist, "futu"
            logger.warning(
                "Futu klines stale/empty for %s (last=%s), falling back to yfinance",
                sym,
                history_last_date(hist),
            )
        except Exception as exc:
            logger.warning("Futu klines failed %s: %s", sym, exc)

    hist = fetch_yfinance_daily_history(sym)
    if hist is not None:
        return hist, "yfinance"
    return None, "none"
