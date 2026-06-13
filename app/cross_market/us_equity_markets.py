"""Fetch and filter Polymarket events relevant to US equities."""
from __future__ import annotations

import re
from typing import Any

from app.cross_market.market_intelligence import get_polymarket_probability
from app.cross_market.polymarket_client import PolymarketClient

# Gamma `/markets?q=` does not search; use `/public-search` with equity-focused queries.
_US_EQUITY_SEARCH_QUERIES: tuple[str, ...] = (
    "NVDA",
    "AAPL",
    "TSLA",
    "earnings",
    "Fed rate",
    "S&P 500",
    "tariff",
    "stock market",
    "recession",
    "interest rate",
)

_EQUITY_KEYWORD_RE = re.compile(
    r"\$[A-Z]{1,5}\b|\([A-Z]{1,5}\)|\bearnings\b|\bfed\b|\bfomc\b|"
    r"rate cut|rate hike|s&p|nasdaq|dow jones|stock market|tariff|inflation|"
    r"\bgdp\b|recession|nvidia|apple|tesla|amazon|microsoft|\bmeta\b|alphabet|\bgoogle\b",
    re.IGNORECASE,
)

_CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})\b")
_PARENS_TICKER_RE = re.compile(r"\(([A-Z]{1,5})\)")

_TICKER_FALSE_POSITIVES = frozenset(
    {
        "US",
        "USA",
        "THE",
        "AND",
        "FOR",
        "ARE",
        "BUT",
        "NOT",
        "YOU",
        "ALL",
        "ANY",
        "CAN",
        "HAD",
        "HER",
        "WAS",
        "ONE",
        "OUR",
        "OUT",
        "DAY",
        "GET",
        "HAS",
        "HIM",
        "HOW",
        "ITS",
        "MAY",
        "NEW",
        "NOW",
        "OLD",
        "SEE",
        "TWO",
        "WAY",
        "WHO",
        "WILL",
        "WITH",
        "THIS",
        "THAT",
        "FROM",
        "HAVE",
        "BEEN",
        "FED",
        "GDP",
        "CPI",
        "IPO",
        "ATH",
        "ATL",
        "YTD",
        "YOY",
        "QOQ",
        "WIN",
        "SAN",
        "BE",
    }
)


def extract_related_ticker(text: str) -> str | None:
    """Only accept explicit cashtags or parenthesized tickers — no loose word matching."""
    upper = text.upper()
    cashtag = _CASHTAG_RE.search(upper)
    if cashtag:
        sym = cashtag.group(1)
        if sym not in _TICKER_FALSE_POSITIVES:
            return sym
    parens = _PARENS_TICKER_RE.search(upper)
    if parens:
        sym = parens.group(1)
        if sym not in _TICKER_FALSE_POSITIVES and 1 <= len(sym) <= 5:
            return sym
    return None


def is_us_equity_related(text: str) -> bool:
    if extract_related_ticker(text):
        return True
    return bool(_EQUITY_KEYWORD_RE.search(text))


def _market_volume(market: dict[str, Any]) -> float:
    for key in ("volume24hr", "volume"):
        try:
            return float(market.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return 0.0


def _normalize_event_market(event: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    title = str(event.get("title") or market.get("question") or "")
    question = str(market.get("question") or title)
    return {
        "id": market.get("id") or event.get("id"),
        "question": question,
        "title": title,
        "endDate": market.get("endDate") or event.get("endDate") or "",
        "slug": market.get("slug") or event.get("slug"),
        "lastTradePrice": market.get("lastTradePrice"),
        "yes_price": market.get("yes_price"),
        "outcomePrices": market.get("outcomePrices"),
        "volume24hr": market.get("volume24hr"),
        "volume": market.get("volume") or event.get("volume"),
        "liquidity": market.get("liquidity") or event.get("liquidity"),
        "related_ticker": extract_related_ticker(f"{title} {question}"),
    }


async def fetch_us_equity_markets(client: PolymarketClient, *, limit: int = 12) -> list[dict[str, Any]]:
    """Aggregate US-equity-related Polymarket rows via public-search."""
    seen_ids: set[str] = set()
    rows: list[dict[str, Any]] = []

    for query in _US_EQUITY_SEARCH_QUERIES:
        events = await client.public_search_events(query=query, limit_per_type=8)
        for event in events:
            markets = event.get("markets") or []
            if not markets:
                continue
            title = str(event.get("title") or "")
            if not is_us_equity_related(title):
                continue
            for market in markets:
                if not market.get("active", True) or market.get("closed"):
                    continue
                market_id = str(market.get("id") or "")
                if not market_id or market_id in seen_ids:
                    continue
                seen_ids.add(market_id)
                rows.append(_normalize_event_market(event, market))

    rows.sort(key=_market_volume, reverse=True)
    return rows[:limit]


def market_to_hot_fields(market: dict[str, Any]) -> dict[str, Any]:
    """Map a normalized market row to hot-event payload fields."""
    question = str(market.get("question") or market.get("title") or "未知事件")
    lower = question.lower()
    if "earnings" in lower:
        event_type = "earnings"
    elif any(w in lower for w in ("fed", "fomc", "rate", "inflation", "gdp", "recession")):
        event_type = "macro_release"
    elif any(w in lower for w in ("war", "tariff", "sanction", "china", "taiwan")):
        event_type = "geopolitical"
    else:
        event_type = "equity"

    pm_prob = get_polymarket_probability(market)
    vol = _market_volume(market)
    try:
        liq = float(market.get("liquidity") or 0)
    except (TypeError, ValueError):
        liq = 0.0

    return {
        "title_zh": question,
        "event_type": event_type,
        "event_time": str(market.get("endDate") or ""),
        "polymarket_probability": round(pm_prob, 3),
        "related_ticker": market.get("related_ticker"),
        "volume_24h": round(vol, 2) if vol > 0 else None,
        "liquidity": round(liq, 2) if liq > 0 else None,
        "slug": market.get("slug"),
    }
