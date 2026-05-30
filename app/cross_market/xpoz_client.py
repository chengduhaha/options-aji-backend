"""Xpoz — social data via official MCP (Streamable HTTP)."""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

import httpx

from app.cross_market.redis_cache import cache_get_json, cache_set_json

logger = logging.getLogger(__name__)

_DEFAULT_MCP_URL = "https://mcp.xpoz.ai/mcp"
_USER_AGENT = "optionsaji-backend/xpoz-mcp"

_POS = frozenset(
    {
        "amazing",
        "beat",
        "bull",
        "bullish",
        "buy",
        "buying",
        "calls",
        "good",
        "great",
        "green",
        "holy",
        "hope",
        "long",
        "love",
        "moon",
        "rally",
        "rich",
        "upside",
    }
)
_NEG = frozenset(
    {
        "bear",
        "bearish",
        "short",
        "puts",
        "crash",
        "dump",
        "red",
        "selloff",
        "fear",
        "bad",
        "loss",
        "miss",
        "downgrade",
        "lawsuit",
        "overvalued",
        "bubble",
    }
)


def _mcp_posts_from_blob(blob: str) -> list[str]:
    if "results[" not in blob:
        return []
    idx = blob.find("results[")
    tail = blob[idx:]
    chunks = tail.split("\n    \"")
    texts: list[str] = []
    for piece in chunks[1:]:
        cut = piece.find('","')
        if cut == -1:
            continue
        raw = piece[:cut].replace("\\n", "\n").replace('\\"', '"')
        texts.append(raw)
    return texts


def _metrics_from_texts(texts: list[str]) -> dict[str, float | int]:
    n = len(texts)
    if n == 0:
        return {"bullish_pct": 50.0, "sentiment_score": 0.0, "mention_count": 0}
    pos_hits = 0
    neg_hits = 0
    for body in texts:
        lower = body.lower()
        pos_hits += sum(1 for w in _POS if re.search(rf"\b{re.escape(w)}\b", lower))
        neg_hits += sum(1 for w in _NEG if re.search(rf"\b{re.escape(w)}\b", lower))
    denom = pos_hits + neg_hits
    if denom == 0:
        return {"bullish_pct": 50.0, "sentiment_score": 0.0, "mention_count": n}
    raw = (pos_hits - neg_hits) / denom
    sentiment_score = max(-1.0, min(1.0, raw))
    bullish_pct = max(0.0, min(100.0, 50.0 * (1.0 + sentiment_score)))
    return {
        "bullish_pct": round(bullish_pct, 2),
        "sentiment_score": round(sentiment_score, 4),
        "mention_count": n,
    }


class XpozClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("XPOZ_API_KEY", "")
        self.mcp_url = os.getenv("XPOZ_MCP_URL", _DEFAULT_MCP_URL).strip() or _DEFAULT_MCP_URL

    async def _twitter_posts_via_mcp(
        self,
        query: str,
        *,
        window_hours: int,
        limit: int = 40,
    ) -> list[str]:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except ImportError:
            logger.warning("xpoz: mcp package not installed")
            return []

        if not query.strip() or window_hours <= 0:
            return []

        end = datetime.now(timezone.utc).date()
        start_dt = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        start = start_dt.date()

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": _USER_AGENT,
        }
        args: dict[str, object] = {
            "query": query,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "responseType": "fast",
            "limit": min(max(limit, 1), 50),
            "filterOutRetweets": True,
            "fields": ["text", "id"],
        }
        timeout = httpx.Timeout(45.0, read=300.0)
        try:
            async with httpx.AsyncClient(headers=headers, timeout=timeout) as http:
                async with streamable_http_client(self.mcp_url, http_client=http) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool("getTwitterPostsByKeywords", args)
        except Exception:
            logger.exception("xpoz MCP getTwitterPostsByKeywords failed query=%s", query[:80])
            return []

        if getattr(result, "isError", False):
            logger.warning("xpoz MCP tool returned isError for query=%s", query[:80])
            return []
        if not result.content:
            return []

        blob = "".join(getattr(block, "text", "") or "" for block in result.content)
        return _mcp_posts_from_blob(blob)

    async def get_ticker_sentiment(self, ticker: str, window_hours: int = 24) -> dict:
        if not self.api_key:
            return {}

        sym = ticker.strip().upper().lstrip("$")
        if not sym:
            return {}

        cache_key = f"xpoz:sentiment:{sym}:{window_hours}"
        cached = await cache_get_json(cache_key)
        if cached is not None:
            return cached

        cashtag = f"${sym}"
        texts = await self._twitter_posts_via_mcp(cashtag, window_hours=window_hours, limit=40)
        metrics = _metrics_from_texts(texts)
        payload: dict[str, object] = {
            "ticker": sym,
            "source": "xpoz_mcp_twitter_keywords",
            "platforms": {"twitter": metrics["mention_count"]},
            **metrics,
        }
        await cache_set_json(cache_key, payload, ttl_seconds=900)
        return payload

    async def search_mentions(self, query: str, platforms: list[str] | None = None) -> list[dict]:
        if not self.api_key or not query.strip():
            return []

        if platforms:
            allowed = {p.lower() for p in platforms}
            if allowed and "twitter" not in allowed:
                return []

        cache_key = f"xpoz:mentions:{query}:{','.join(platforms or [])}"
        cached = await cache_get_json(cache_key)
        if cached is not None:
            return cached

        texts = await self._twitter_posts_via_mcp(query.strip(), window_hours=72, limit=30)
        payload = [{"text": t, "platform": "twitter"} for t in texts]
        await cache_set_json(cache_key, payload, ttl_seconds=900)
        return payload

    async def close(self) -> None:
        return
