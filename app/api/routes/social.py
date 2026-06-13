"""Social radar and smart-vs-retail endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import bearer_subscription_optional
from app.config import get_settings
from app.services.cache_service import cache_get, cache_set
from app.services.locale import parse_locale, pick_text
from app.services.social_sentiment import (
    KolDirectoryResponse,
    ResonanceStreamResponse,
    SmartVsRetailSnapshot,
    build_smart_vs_retail,
    get_kol_directory,
    get_social_radar,
    list_resonance_timeline,
)

router = APIRouter(prefix="/api/social", tags=["social"])
SOCIAL_ROUTE_TTL_SECONDS = 300


@router.get("/kol")
def social_kol_directory(
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    cfg = get_settings()
    if not cfg.feature_social_enabled:
        return KolDirectoryResponse(generated_at_utc="", items=[]).model_dump()
    payload = get_kol_directory()
    return payload.model_dump()


@router.get("/resonance")
def social_resonance_stream(
    limit: int = Query(default=30, ge=1, le=100),
    symbol: Optional[str] = Query(default=None, description="Filter by underlying symbol"),
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    cfg = get_settings()
    if not cfg.feature_social_enabled:
        return ResonanceStreamResponse(generated_at_utc="", items=[]).model_dump()
    payload = list_resonance_timeline(limit=limit, symbol=symbol)
    return payload.model_dump()


@router.get("/radar")
def social_radar(
    limit: int = Query(default=10, ge=1, le=50),
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    cfg = get_settings()
    if not cfg.feature_social_enabled:
        return {"generated_at_utc": "", "items": []}
    cache_key = f"social:radar:v1:{limit}"
    cached = cache_get(cache_key)
    if isinstance(cached, dict):
        return cached
    payload = get_social_radar(limit=limit)
    body = payload.model_dump()
    cache_set(cache_key, body, ttl=SOCIAL_ROUTE_TTL_SECONDS)
    return body


@router.get("/smart-vs-retail/{symbol}", response_model=SmartVsRetailSnapshot)
def smart_vs_retail(
    symbol: str,
    locale: str = Query(default="zh", pattern="^(zh|en)$"),
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> SmartVsRetailSnapshot:
    loc = parse_locale(locale)
    cfg = get_settings()
    if not cfg.feature_social_enabled:
        return SmartVsRetailSnapshot(
            symbol=symbol.upper(),
            snapshot_time="",
            institutional_direction="neutral",
            institutional_strength=0,
            unusual_flow_count_24h=0,
            premium_flow_usd=0,
            retail_direction="neutral",
            retail_sentiment_score=50,
            mentions_24h=0,
            mention_growth_pct=0.0,
            consensus_type="neutral",
            ai_narrative_zh="social feature disabled",
            confidence=0.0,
        )
    sym = symbol.strip().upper()
    cache_key = f"social:smart-vs-retail:v1:{sym}"
    cached = cache_get(cache_key)
    if isinstance(cached, dict):
        snapshot = SmartVsRetailSnapshot.model_validate(cached)
    else:
        snapshot = build_smart_vs_retail(sym)
        cache_set(cache_key, snapshot.model_dump(), ttl=SOCIAL_ROUTE_TTL_SECONDS)
    narrative = pick_text(
        zh=snapshot.ai_narrative_zh,
        en=getattr(snapshot, "ai_narrative_en", None),
        locale=loc,
    )
    return snapshot.model_copy(update={"ai_narrative_zh": narrative})
