"""Cross-market Polymarket and Xpoz feature tests."""
from __future__ import annotations

import pytest


def test_us_equity_market_filter_requires_explicit_ticker_or_equity_keyword() -> None:
    from app.cross_market.us_equity_markets import (
        extract_related_ticker,
        is_us_equity_related,
        market_to_hot_fields,
    )

    assert extract_related_ticker("Will $NVDA close above 150?") == "NVDA"
    assert extract_related_ticker("Will NVIDIA (NVDA) beat earnings?") == "NVDA"
    assert extract_related_ticker("Will the US win?") is None
    assert is_us_equity_related("Fed rate cut in 2026") is True
    assert is_us_equity_related("Random sports market") is False

    fields = market_to_hot_fields(
        {
            "id": "m1",
            "question": "Will NVIDIA (NVDA) beat earnings?",
            "endDate": "2026-07-01T00:00:00Z",
            "outcomePrices": ["0.62", "0.38"],
            "volume24hr": "12345.6",
            "liquidity": "1000",
            "related_ticker": "NVDA",
        }
    )

    assert fields["event_type"] == "earnings"
    assert fields["polymarket_probability"] == 0.62
    assert fields["related_ticker"] == "NVDA"
    assert fields["volume_24h"] == 12345.6


@pytest.mark.asyncio
async def test_xpoz_hot_returns_unconfigured_without_api_key(monkeypatch) -> None:
    import app.config as config_mod
    from app.cross_market.xpoz_us_hot import fetch_xpoz_us_hot

    monkeypatch.setattr(
        config_mod,
        "get_settings",
        lambda: type("Cfg", (), {"xpoz_api_key": ""})(),
    )

    response = await fetch_xpoz_us_hot(limit=5)

    assert response.configured is False
    assert response.items == []
