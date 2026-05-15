"""Social radar and smart-vs-retail endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import bearer_subscription_optional
from app.config import get_settings
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
    payload = get_social_radar(limit=limit)
    return payload.model_dump()


@router.get("/smart-vs-retail/{symbol}", response_model=SmartVsRetailSnapshot)
def smart_vs_retail(
    symbol: str,
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> SmartVsRetailSnapshot:
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
    return build_smart_vs_retail(symbol)
