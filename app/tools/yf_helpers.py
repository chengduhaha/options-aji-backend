"""Shared yfinance helpers — use library default session (curl_cffi when available)."""

from __future__ import annotations

import yfinance as yf


def yf_ticker(symbol: str) -> yf.Ticker:
    """Construct a :class:`yfinance.Ticker` without a custom requests session.

    Recent yfinance versions require curl_cffi-backed sessions; passing a plain
  ``requests.Session`` raises at runtime. Timeouts are handled by callers.
    """
    sym = symbol.strip().upper()
    return yf.Ticker(sym)
