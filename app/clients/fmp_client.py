"""Financial Modeling Prep (FMP) API client.

Base URL: https://financialmodelingprep.com/stable
Auth: ?apikey={API_KEY}
Rate limit: 300 calls/min on Starter tier
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0


class FMPClient:
    """Thin synchronous wrapper around FMP Stable API."""

    def __init__(self, api_key: str, base_url: str = "https://financialmodelingprep.com/stable"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    # ── low-level ──────────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None, retries: int = 2) -> Any:
        url = f"{self.base_url}{path}"
        p = {"apikey": self.api_key, **(params or {})}
        for attempt in range(retries + 1):
            try:
                with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
                    resp = client.get(url, params=p)
                    if resp.status_code == 429:
                        wait = float(resp.headers.get("Retry-After", "2"))
                        logger.warning("FMP rate limited, waiting %.1fs", wait)
                        time.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return resp.json()
            except httpx.HTTPStatusError as exc:
                if attempt == retries:
                    logger.warning("FMP HTTP %s %s: %s", exc.response.status_code, path, exc.response.text[:200])
                    return None
            except Exception as exc:
                if attempt == retries:
                    logger.warning("FMP request failed %s: %s", path, exc)
                    return None
                time.sleep(1)
        return None

    # ── Company Info ───────────────────────────────────────────────────────────

    def get_profile(self, symbol: str) -> Optional[dict]:
        data = self._get("/profile", {"symbol": symbol.upper()})
        if isinstance(data, list) and data:
            return data[0]
        return None

    def get_peers(self, symbol: str) -> list[str]:
        data = self._get("/stock-peers", {"symbol": symbol.upper()})
        if isinstance(data, list) and data:
            return data[0].get("peersList", [])
        return []

    def get_executives(self, symbol: str) -> list[dict]:
        return self._get("/key-executives", {"symbol": symbol.upper()}) or []

    def search_symbol(self, query: str, limit: int = 10) -> list[dict]:
        return self._get("/search-symbol", {"query": query, "limit": limit}) or []

    def search_name(self, query: str, limit: int = 10) -> list[dict]:
        return self._get("/search-name", {"query": query, "limit": limit}) or []

    # ── Quotes ─────────────────────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> Optional[dict]:
        data = self._get("/quote", {"symbol": symbol.upper()})
        if isinstance(data, list) and data:
            return data[0]
        return None

    def get_batch_quote_short(self, symbols: list[str]) -> list[dict]:
        """Up to 100 symbols per call."""
        joined = ",".join(s.upper() for s in symbols)
        return self._get("/batch-quote-short", {"symbols": joined}) or []

    def get_quote_change(self, symbol: str) -> Optional[dict]:
        data = self._get("/quote-change", {"symbol": symbol.upper()})
        if isinstance(data, list) and data:
            return data[0]
        return None

    def get_aftermarket_quote(self, symbol: str) -> Optional[dict]:
        data = self._get("/aftermarket-quote", {"symbol": symbol.upper()})
        if isinstance(data, list) and data:
            return data[0]
        return None

    # ── Financial Statements ───────────────────────────────────────────────────────

    def get_income_statement(self, symbol: str, period: str = "quarter", limit: int = 8) -> list[dict]:
        return self._get("/income-statement", {"symbol": symbol.upper(), "period": period, "limit": limit}) or []

    def get_balance_sheet(self, symbol: str, period: str = "quarter", limit: int = 8) -> list[dict]:
        return self._get("/balance-sheet-statement", {"symbol": symbol.upper(), "period": period, "limit": limit}) or []

    def get_cash_flow(self, symbol: str, period: str = "quarter", limit: int = 8) -> list[dict]:
        return self._get("/cash-flow-statement", {"symbol": symbol.upper(), "period": period, "limit": limit}) or []

    def get_key_metrics(self, symbol: str, period: str = "quarter", limit: int = 8) -> list[dict]:
        return self._get("/key-metrics", {"symbol": symbol.upper(), "period": period, "limit": limit}) or []

    def get_key_metrics_ttm(self, symbol: str) -> Optional[dict]:
        data = self._get("/key-metrics-ttm", {"symbol": symbol.upper()})
        if isinstance(data, list) and data:
            return data[0]
        return None

    def get_financial_ratios(self, symbol: str, period: str = "quarter", limit: int = 8) -> list[dict]:
        return self._get("/financial-ratios", {"symbol": symbol.upper(), "period": period, "limit": limit}) or []

    def get_financial_ratios_ttm(self, symbol: str) -> Optional[dict]:
        data = self._get("/financial-ratios-ttm", {"symbol": symbol.upper()})
        if isinstance(data, list) and data:
            return data[0]
        return None

    def get_financial_scores(self, symbol: str) -> Optional[dict]:
        data = self._get("/financial-scores", {"symbol": symbol.upper()})
        if isinstance(data, list) and data:
            return data[0]
        return None

    def get_dcf(self, symbol: str) -> Optional[dict]:
        data = self._get("/discounted-cash-flow", {"symbol": symbol.upper()})
        if isinstance(data, list) and data:
            return data[0]
        return None

    # ── Earnings ────────────────────────────────────────────────────────────────

    def get_earnings_calendar(self, from_date: str, to_date: str) -> list[dict]:
        return self._get("/earnings-calendar", {"from": from_date, "to": to_date}) or []

    def get_earnings_surprises(self, symbol: str) -> list[dict]:
        return self._get(f"/earnings-surprises/{symbol.upper()}") or []

    def get_earnings_report(self, symbol: str) -> Optional[dict]:
        data = self._get("/earnings-report", {"symbol": symbol.upper()})
        if isinstance(data, list) and data:
            return data[0]
        return None

    # ── Historical Price ─────────────────────────────────────────────────────────

    def get_historical_price_eod(self, symbol: str, from_date: str = "", to_date: str = "") -> list[dict]:
        params: dict = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        data = self._get(f"/historical-price-eod/full/{symbol.upper()}", params)
        if isinstance(data, dict):
            return data.get("historical", [])
        return []

    def get_intraday_chart(self, symbol: str, interval: str = "5min", from_date: str = "", to_date: str = "") -> list[dict]:
        """interval: 1min, 5min, 15min, 30min, 1hour, 4hour"""
        params: dict = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        return self._get(f"/historical-chart/{interval}/{symbol.upper()}", params) or []

    # ── Market Intelligence ─────────────────────────────────────────────────────

    def get_stock_news(self, tickers: list[str] = [], page: int = 0, limit: int = 20) -> list[dict]:
        params: dict = {"page": page, "limit": limit}
        if tickers:
            params["tickers"] = ",".join(t.upper() for t in tickers)
        return self._get("/news/stock", params) or []

    def search_news(self, query: str, page: int = 0) -> list[dict]:
        return self._get("/search-news/stock", {"query": query, "page": page}) or []

    def get_insider_trading(self, symbol: str, page: int = 0, limit: int = 100) -> list[dict]:
        return self._get("/insider-trading", {"symbol": symbol.upper(), "page": page, "limit": limit}) or []

    def get_insider_trading_latest(self, page: int = 0) -> list[dict]:
        return self._get("/insider-trading", {"page": page}) or []

    def get_analyst_ratings(self, symbol: str) -> list[dict]:
        return self._get("/stock-grades", {"symbol": symbol.upper()}) or []

    def get_price_target_summary(self, symbol: str) -> Optional[dict]:
        data = self._get("/price-target-summary", {"symbol": symbol.upper()})
        if isinstance(data, list) and data:
            return data[0]
        return None

    def get_price_target_consensus(self, symbol: str) -> Optional[dict]:
        data = self._get("/price-target-consensus", {"symbol": symbol.upper()})
        if isinstance(data, list) and data:
            return data[0]
        return None

    # ── Market Performance ───────────────────────────────────────────────────────

    def get_sector_performance(self) -> list[dict]:
        return self._get("/sector-performance") or []

    def get_industry_performance(self) -> list[dict]:
        return self._get("/industry-performance") or []

    def get_gainers(self) -> list[dict]:
        return self._get("/stock-market-gainers") or []

    def get_losers(self) -> list[dict]:
        return self._get("/stock-market-losers") or []

    def get_most_actives(self) -> list[dict]:
        return self._get("/stock-market-most-actives") or []

    def get_sector_pe(self) -> list[dict]:
        return self._get("/sector-pe-snapshot") or []

    # ── Macro Economics ───────────────────────────────────────────────────────

    def get_economic_calendar(self, from_date: str, to_date: str) -> list[dict]:
        return self._get("/economic-calendar", {"from": from_date, "to": to_date}) or []

    def get_treasury_rates(self, from_date: str = "", to_date: str = "") -> list[dict]:
        params: dict = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        return self._get("/treasury-rates", params) or []

    def get_economic_indicator(self, name: str) -> list[dict]:
        return self._get("/economics-indicators", {"name": name}) or []

    def get_market_risk_premium(self) -> list[dict]:
        return self._get("/market-risk-premium") or []

    # ── Indices ─────────────────────────────────────────────────────────────────

    def get_index_quote(self, symbol: str) -> Optional[dict]:
        data = self._get("/index-quote", {"symbol": symbol})
        if isinstance(data, list) and data:
            return data[0]
        return None

    def get_all_index_quotes(self) -> list[dict]:
        return self._get("/all-index-quotes") or []

    def get_sp500_components(self) -> list[dict]:
        return self._get("/sp500-index") or []

    def get_nasdaq_components(self) -> list[dict]:
        return self._get("/nasdaq-index") or []

    def get_dow_components(self) -> list[dict]:
        return self._get("/dow-jones-index") or []

    def get_market_hours(self) -> Optional[dict]:
        return self._get("/is-the-market-open")

    # ── Congress / Insider ────────────────────────────────────────────────────

    def get_senate_latest_trading(self, page: int = 0) -> list[dict]:
        return self._get("/senate-latest-trading", {"page": page}) or []

    def get_house_latest_trading(self, page: int = 0) -> list[dict]:
        return self._get("/house-latest-trading", {"page": page}) or []

    def get_senate_trading_by_symbol(self, symbol: str) -> list[dict]:
        return self._get("/senate-trading-activity", {"symbol": symbol.upper()}) or []

    def get_house_trading_by_symbol(self, symbol: str) -> list[dict]:
        return self._get("/senate-trading-activity", {"symbol": symbol.upper()}) or []

    # ── ETF ───────────────────────────────────────────────────────────────────

    def get_etf_holdings(self, symbol: str) -> list[dict]:
        return self._get("/etf-holdings", {"symbol": symbol.upper()}) or []

    def get_etf_info(self, symbol: str) -> Optional[dict]:
        data = self._get("/etf-mutual-fund-info", {"symbol": symbol.upper()})
        if isinstance(data, list) and data:
            return data[0]
        return None

    def get_etf_sector_weighting(self, symbol: str) -> list[dict]:
        return self._get("/etf-sector-weighting", {"symbol": symbol.upper()}) or []

    def get_etf_country_allocation(self, symbol: str) -> list[dict]:
        return self._get("/etf-country-allocation", {"symbol": symbol.upper()}) or []

    def get_etf_list(self) -> list[dict]:
        return self._get("/etf-list") or []

    # ── SEC Filings ─────────────────────────────────────────────────────────────

    def get_sec_filings(self, symbol: str, type_: str = "", page: int = 0) -> list[dict]:
        params: dict = {"symbol": symbol.upper(), "page": page}
        if type_:
            params["type"] = type_
        return self._get("/sec-filings-by-symbol", params) or []

    def get_sec_latest_8k(self, page: int = 0) -> list[dict]:
        return self._get("/sec-filings-latest-8k", {"page": page}) or []

    # ── Technical Indicators ──────────────────────────────────────────────────────

    def get_technical_indicator(
        self, symbol: str, indicator_type: str = "ema", period: int = 20, timeframe: str = "daily"
    ) -> list[dict]:
        return self._get(
            f"/technical-indicator/{timeframe}/{symbol.upper()}",
            {"type": indicator_type, "period": period}
        ) or []


def get_fmp_client() -> FMPClient:
    cfg = get_settings()
    return FMPClient(api_key=cfg.fmp_api_key, base_url=cfg.fmp_base_url)
