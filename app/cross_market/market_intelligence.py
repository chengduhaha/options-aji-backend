"""Build four-source probabilities for one Polymarket row."""
from __future__ import annotations

import asyncio
import logging
import re

from app.clients.fmp_client import get_fmp_client
from app.cross_market.massive_utils import get_options_chain_results
from app.cross_market.xpoz_client import XpozClient

logger = logging.getLogger(__name__)

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
        "BOY",
        "DID",
        "LET",
        "PUT",
        "END",
        "SAY",
        "SHE",
        "TOO",
        "USE",
        "MAN",
        "TRY",
        "ASK",
        "BIG",
        "GOT",
        "OWN",
        "OFF",
        "TOP",
        "LOW",
        "YES",
        "WAR",
        "RED",
        "GOP",
        "DEM",
        "JOB",
        "EPS",
        "IMO",
        "IPO",
        "ATH",
        "ATL",
        "GDP",
        "CPI",
        "FED",
        "EOM",
        "YTD",
        "YOY",
        "QOQ",
        "WILL",
        "WITH",
        "THIS",
        "THAT",
        "FROM",
        "HAVE",
        "BEEN",
        "MORE",
        "THAN",
        "WHEN",
        "WHAT",
        "YOUR",
        "INTO",
        "ONLY",
        "OVER",
        "SUCH",
        "ALSO",
        "VERY",
        "JUST",
        "YEAR",
        "TIME",
        "DOWN",
        "BACK",
        "EVEN",
        "WORK",
        "LONG",
        "LAST",
        "MADE",
        "MANY",
        "MOST",
        "MUCH",
        "NEED",
        "NEXT",
        "OPEN",
        "PART",
        "RATE",
        "REAL",
        "SAME",
        "SEEM",
        "SHOW",
        "SOME",
        "TAKE",
        "TEAM",
        "THEM",
        "THEN",
        "THEY",
        "UPON",
        "USED",
        "WANT",
        "WEEK",
        "WELL",
        "WENT",
        "WERE",
    }
)


def clamp01(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def extract_primary_ticker(text: str) -> str | None:
    upper = text.upper()
    dollar_hits = re.findall(r"\$([A-Z]{1,5})\b", upper)
    for sym in dollar_hits:
        if sym not in _TICKER_FALSE_POSITIVES and 1 <= len(sym) <= 5:
            return sym[:5]

    matches = re.findall(r"\b([A-Z]{2,5})\b", upper)
    if not matches:
        return None
    for candidate in matches:
        if candidate in _TICKER_FALSE_POSITIVES:
            continue
        return candidate
    return None


def get_polymarket_probability(market: dict) -> float:
    for key in ("lastTradePrice", "yes_price"):
        try:
            value = float(market.get(key))
            if 0 <= value <= 1:
                return value
        except Exception:
            continue
    outcome_prices = market.get("outcomePrices")
    if isinstance(outcome_prices, list) and outcome_prices:
        try:
            return clamp01(float(outcome_prices[0]))
        except Exception:
            pass
    return 0.5


async def infer_options_probability(ticker: str | None) -> float:
    if not ticker:
        return 0.5

    chain = await get_options_chain_results(ticker)
    try:
        results = chain.get("results", [])
        if not results:
            return 0.5
        call_volume = 0.0
        put_volume = 0.0
        for row in results:
            details = row.get("details", {})
            option_type = str(details.get("contract_type") or details.get("option_type") or "").lower()
            day = row.get("day", {})
            volume = float(day.get("volume") or row.get("volume") or 0)
            if option_type == "call":
                call_volume += volume
            elif option_type == "put":
                put_volume += volume
        total = call_volume + put_volume
        if total <= 0:
            return 0.5
        return clamp01(call_volume / total)
    except Exception:
        logger.debug("options inference fallback for %s", ticker, exc_info=True)
        return 0.5


async def infer_social_probability(ticker: str | None) -> float:
    if not ticker:
        return 0.5
    client = XpozClient()
    try:
        data = await client.get_ticker_sentiment(ticker=ticker, window_hours=24)
        if not data:
            return 0.5
        if "bullish_pct" in data:
            return clamp01(float(data.get("bullish_pct", 50)) / 100.0)
        score = float(data.get("sentiment_score", 0))
        return clamp01((score + 1.0) / 2.0)
    except Exception:
        logger.debug("social (Xpoz) inference fallback for %s", ticker, exc_info=True)
        return 0.5
    finally:
        await client.close()


async def infer_institutional_probability(ticker: str | None) -> float:
    if not ticker:
        return 0.5

    def _fetch() -> list[dict]:
        return get_fmp_client().get_insider_trades(ticker or "")

    try:
        trades = await asyncio.to_thread(_fetch)
        if not trades:
            return 0.5
        net = 0.0
        for trade in trades[:50]:
            value = float(trade.get("securitiesTransacted") or trade.get("value") or 0)
            transaction = str(trade.get("transactionType") or "").lower()
            if "buy" in transaction or "purchase" in transaction:
                net += value
            elif "sell" in transaction or "sale" in transaction:
                net -= value
        if net == 0:
            return 0.5
        scale = min(abs(net) / 1_000_000, 1.0) * 0.25
        return clamp01(0.5 + scale if net > 0 else 0.5 - scale)
    except Exception:
        logger.debug("institutional (FMP) inference fallback for %s", ticker, exc_info=True)
        return 0.5


async def enrich_probabilities_for_market(market: dict) -> dict:
    question = str(market.get("question") or "")
    ticker = extract_primary_ticker(question)
    polymarket_probability = get_polymarket_probability(market)
    options_probability = await infer_options_probability(ticker)
    social_probability = await infer_social_probability(ticker)
    institutional_probability = await infer_institutional_probability(ticker)
    return {
        "ticker": ticker,
        "polymarket_probability": polymarket_probability,
        "options_probability": options_probability,
        "social_probability": social_probability,
        "institutional_probability": institutional_probability,
    }
