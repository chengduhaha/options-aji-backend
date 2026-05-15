"""Tests for social KOL handle parsing and feed helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from typing import Optional

from app.db.models import SocialPostRow
from app.services.kol_handles import parse_kol_handles_csv
from app.services.resonance_feed import resonance_stream_to_feed_fields, social_row_matches_kol_filter


def test_parse_kol_handles_csv_normalizes_and_dedupes() -> None:
    raw = " @Unusual_Whales , unusual_whales , OptionsHawk , "
    assert parse_kol_handles_csv(raw) == ["unusual_whales", "optionshawk"]


def test_social_row_kol_filter_requires_tracked_when_kol_only() -> None:
    kol_set = {"foo"}
    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    row = SocialPostRow(
        source="twitter",
        external_id="1",
        tickers=["NVDA"],
        raw_json={"kol_tracked": True, "kol_handle": "foo"},
        created_at=ts,
    )
    assert social_row_matches_kol_filter(sr=row, kol_only=True, kol_set=kol_set) is True

    row2 = SocialPostRow(
        source="twitter",
        external_id="2",
        author="random",
        tickers=["NVDA"],
        raw_json={},
        created_at=ts,
    )
    assert social_row_matches_kol_filter(sr=row2, kol_only=True, kol_set=kol_set) is False
    assert social_row_matches_kol_filter(sr=row2, kol_only=False, kol_set=kol_set) is True


@dataclass
class _ResRow:
    id: int
    symbol: str
    signal_type: str
    triggered_at_utc: str
    institutional_direction: str
    retail_direction: str
    institutional_strength: int
    retail_strength: int
    confidence: Optional[float]
    narrative_zh: Optional[str]


def test_resonance_feed_item_maps_sentiment() -> None:
    ritem = _ResRow(
        id=9,
        symbol="NVDA",
        signal_type="resonance",
        triggered_at_utc="2025-05-01T12:00:00+00:00",
        institutional_direction="bullish",
        retail_direction="bullish",
        institutional_strength=3,
        retail_strength=4,
        confidence=0.8,
        narrative_zh="同向看涨",
    )
    fi = resonance_stream_to_feed_fields(ritem)
    assert fi.kind == "resonance"
    assert fi.sentiment == "bullish"
    assert fi.priority == "high"
    assert fi.tickers == ["NVDA"]
