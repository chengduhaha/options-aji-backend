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


@pytest.mark.asyncio
async def test_xpoz_hot_uses_fallback_when_live_fetch_times_out(monkeypatch) -> None:
    import app.config as config_mod
    import app.cross_market.xpoz_us_hot as xpoz_mod

    monkeypatch.setattr(
        config_mod,
        "get_settings",
        lambda: type("Cfg", (), {"xpoz_api_key": "key"})(),
    )
    monkeypatch.setattr(xpoz_mod, "US_WATCHLIST", ("NVDA", "TSLA"))
    monkeypatch.setattr(xpoz_mod, "_PER_SYMBOL_TIMEOUT_SECONDS", 0.01)

    def _slow_fetch(_symbol: str):
        import time

        time.sleep(0.05)
        return None

    monkeypatch.setattr(xpoz_mod, "_fetch_xpoz_sentiment", _slow_fetch)

    response = await xpoz_mod.fetch_xpoz_us_hot(limit=2)

    assert response.configured is True
    assert response.source == "xpoz+fallback"
    assert [item.ticker for item in response.items] == ["TSLA", "NVDA"]
    assert all(item.mentions_24h > 0 for item in response.items)


def test_xpoz_hot_fetch_budget_is_small_enough_for_page_navigation() -> None:
    import app.cross_market.xpoz_us_hot as xpoz_mod

    worst_case_batches = (len(xpoz_mod.US_WATCHLIST) + xpoz_mod._CONCURRENT_FETCHES - 1) // xpoz_mod._CONCURRENT_FETCHES
    worst_case_seconds = worst_case_batches * xpoz_mod._PER_SYMBOL_TIMEOUT_SECONDS

    assert worst_case_seconds <= 3.0


@pytest.mark.asyncio
async def test_xpoz_ticker_detail_uses_fallback_when_live_fetch_times_out(monkeypatch) -> None:
    import app.config as config_mod
    import app.cross_market.xpoz_ticker_detail as detail_mod

    monkeypatch.setattr(
        config_mod,
        "get_settings",
        lambda: type("Cfg", (), {"xpoz_api_key": "key"})(),
    )
    monkeypatch.setattr(detail_mod, "_PER_SYMBOL_TIMEOUT_SECONDS", 0.01)

    def _slow_fetch(_symbol: str):
        import time

        time.sleep(0.05)
        return None

    monkeypatch.setattr(detail_mod, "_fetch_xpoz_sentiment", _slow_fetch)

    response = await detail_mod.fetch_xpoz_ticker_detail("GOOGL")

    assert response.configured is True
    assert response.symbol == "GOOGL"
    assert response.mentions_24h > 0
    assert response.direction in {"bullish", "bearish", "neutral"}
