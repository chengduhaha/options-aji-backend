"""Rank US equities by Xpoz Twitter/Reddit mention volume (standalone from Polymarket)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.cross_market.redis_cache import cache_get_json, cache_set_json
from app.services.social_sentiment import _fetch_xpoz_sentiment

logger = logging.getLogger(__name__)

# Liquid / meme / macro-sensitive US names — scanned in parallel, ranked by mentions.
US_WATCHLIST: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "IWM",
    "NVDA",
    "TSLA",
    "AAPL",
    "AMZN",
    "MSFT",
    "META",
    "AMD",
    "GOOGL",
    "PLTR",
    "COIN",
    "MSTR",
    "SMCI",
    "ARM",
    "AVGO",
    "NFLX",
    "MU",
    "GME",
)


class XpozHotItem(BaseModel):
    rank: int
    ticker: str
    mentions_24h: int = 0
    mention_growth_pct: float = 0.0
    sentiment_score: int = Field(default=50, ge=0, le=100)
    direction: str = "neutral"
    twitter_mentions: int = 0
    reddit_mentions: int = 0
    sample_posts: list[str] = Field(default_factory=list)


class XpozHotResponse(BaseModel):
    generated_at_utc: str
    source: str = "xpoz"
    configured: bool
    items: list[XpozHotItem]


def _direction_from_score(score: int) -> str:
    if score >= 60:
        return "bullish"
    if score <= 40:
        return "bearish"
    return "neutral"


def _sample_snippets(posts: list[object], *, limit: int = 2) -> list[str]:
    out: list[str] = []
    for post in posts:
        title = getattr(post, "title", None) or ""
        content = getattr(post, "content", None) or ""
        text = f"{title} {content}".strip()
        if not text:
            continue
        snippet = text.replace("\n", " ")[:180]
        if snippet:
            out.append(snippet)
        if len(out) >= limit:
            break
    return out


async def fetch_xpoz_us_hot(*, limit: int = 15) -> XpozHotResponse:
    from app.config import get_settings

    now = datetime.now(timezone.utc).isoformat()
    api_key = get_settings().xpoz_api_key.strip()
    if not api_key:
        return XpozHotResponse(generated_at_utc=now, configured=False, items=[])

    cache_key = f"cm:xpoz:us_hot:{limit}"
    cached = await cache_get_json(cache_key)
    if isinstance(cached, dict) and cached.get("items"):
        return XpozHotResponse.model_validate(cached)

    sem = asyncio.Semaphore(4)

    async def _one(sym: str) -> XpozHotItem | None:
        async with sem:
            try:
                result = await asyncio.to_thread(_fetch_xpoz_sentiment, sym)
            except Exception:
                logger.debug("xpoz hot fetch failed for %s", sym, exc_info=True)
                return None
        if result is None:
            return None
        tw = int((result.source_breakdown or {}).get("twitter", 0))
        rd = int((result.source_breakdown or {}).get("reddit", 0))
        return XpozHotItem(
            rank=0,
            ticker=sym,
            mentions_24h=int(result.mentions_24h),
            mention_growth_pct=float(result.mentions_growth_pct or 0),
            sentiment_score=int(result.sentiment_score),
            direction=_direction_from_score(int(result.sentiment_score)),
            twitter_mentions=tw,
            reddit_mentions=rd,
            sample_posts=_sample_snippets(result.posts),
        )

    rows = await asyncio.gather(*[_one(sym) for sym in US_WATCHLIST])
    items = [r for r in rows if r is not None and r.mentions_24h > 0]
    items.sort(key=lambda x: (x.mentions_24h, x.mention_growth_pct), reverse=True)
    ranked = [item.model_copy(update={"rank": idx + 1}) for idx, item in enumerate(items[:limit])]

    payload = XpozHotResponse(
        generated_at_utc=now,
        configured=True,
        items=ranked,
    )
    await cache_set_json(cache_key, payload.model_dump(), ttl_seconds=900)
    return payload
