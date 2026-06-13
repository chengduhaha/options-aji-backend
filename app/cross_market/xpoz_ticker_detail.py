"""Per-ticker social heat detail from X/Twitter + Reddit."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.config import get_settings
from app.cross_market.redis_cache import cache_get_json, cache_set_json
from app.services.social_sentiment import SocialPostPayload, _fetch_xpoz_sentiment

logger = logging.getLogger(__name__)
_CACHE_TTL = 900


class SocialPostItem(BaseModel):
    source: str
    author: str | None = None
    title: str | None = None
    content: str | None = None
    url: str | None = None
    score: int | None = None
    comments_count: int | None = None
    created_at: str


class XpozTickerDetailResponse(BaseModel):
    symbol: str
    generated_at_utc: str
    configured: bool
    mentions_24h: int = 0
    mention_growth_pct: float = 0.0
    sentiment_score: int = 50
    direction: str = "neutral"
    twitter_mentions: int = 0
    reddit_mentions: int = 0
    posts: list[SocialPostItem] = Field(default_factory=list)


def _direction_from_score(score: int) -> str:
    if score >= 60:
        return "bullish"
    if score <= 40:
        return "bearish"
    return "neutral"


def _post_to_item(post: SocialPostPayload) -> SocialPostItem:
    created = post.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return SocialPostItem(
        source=post.source,
        author=post.author,
        title=post.title,
        content=post.content,
        url=post.url,
        score=post.score,
        comments_count=post.comments_count,
        created_at=created.isoformat(),
    )


async def fetch_xpoz_ticker_detail(symbol: str) -> XpozTickerDetailResponse:
    sym = symbol.strip().upper()
    now = datetime.now(timezone.utc).isoformat()
    api_key = get_settings().xpoz_api_key.strip()
    if not api_key:
        return XpozTickerDetailResponse(symbol=sym, generated_at_utc=now, configured=False)

    cache_key = f"xpoz:ticker_detail:v1:{sym}"
    if cached := cache_get_json(cache_key):
        try:
            return XpozTickerDetailResponse.model_validate(cached)
        except Exception:
            logger.debug("xpoz ticker detail cache invalid symbol=%s", sym)

    result = _fetch_xpoz_sentiment(sym)
    if result is None:
        payload = XpozTickerDetailResponse(symbol=sym, generated_at_utc=now, configured=True)
        cache_set_json(cache_key, payload.model_dump(), ttl_seconds=_CACHE_TTL)
        return payload

    breakdown = result.source_breakdown or {}
    posts = sorted(result.posts, key=lambda p: p.created_at, reverse=True)
    payload = XpozTickerDetailResponse(
        symbol=sym,
        generated_at_utc=now,
        configured=True,
        mentions_24h=result.mentions_24h,
        mention_growth_pct=result.mentions_growth_pct,
        sentiment_score=result.sentiment_score,
        direction=_direction_from_score(result.sentiment_score),
        twitter_mentions=int(breakdown.get("twitter") or 0),
        reddit_mentions=int(breakdown.get("reddit") or 0),
        posts=[_post_to_item(p) for p in posts[:80]],
    )
    cache_set_json(cache_key, payload.model_dump(), ttl_seconds=_CACHE_TTL)
    return payload
