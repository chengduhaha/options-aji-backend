"""Performance guardrails for feed aggregation routes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

from app.api.routes import feed_unified, signals_feed
from app.api.routes import market_dashboard
from app.api.routes import mvp as mvp_route
from app.api.routes import social as social_route
from app.services import mvp_market_context


def test_unified_feed_discord_filter_skips_signal_builder(monkeypatch) -> None:
    """A Discord-only timeline should not compute live market signals first."""

    def fail_signals(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("signals_feed should not run for kind=discord")

    monkeypatch.setattr(feed_unified, "signals_feed", fail_signals)
    monkeypatch.setattr(feed_unified, "resolve_author_filter", lambda *_a, **_k: None)
    monkeypatch.setattr(feed_unified, "fetch_macro_calendar_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(feed_unified, "list_resonance_timeline", lambda *_a, **_k: SimpleNamespace(items=[]))
    monkeypatch.setattr(feed_unified, "kol_handle_set_from_settings", lambda: set())

    row = SimpleNamespace(
        id="row-1",
        timestamp_utc_iso="2026-06-13T00:00:00+00:00",
        author="Discord",
        content="SPY update",
        tickers=["SPY"],
        enrichment_title_zh=None,
        enrichment_summary_zh=None,
        enrichment_bullets_zh=(),
        enrichment_risk_zh=None,
        enrichment_lang=None,
    )
    monkeypatch.setattr(feed_unified, "list_discord_feed_rows", lambda *_a, **_k: [row])

    env = feed_unified.unified_feed_timeline(
        ticker=None,
        kind="discord",
        sentiment=None,
        priority=None,
        limit=50,
        kol_only=False,
        before_timestamp=None,
        hours=6,
        menu_slot="feed",
        locale="zh",
        session=SimpleNamespace(),
        _=None,
    )

    assert [item.kind for item in env.items] == ["discord"]


def test_signals_feed_uses_cache(monkeypatch) -> None:
    """Repeated signal feed calls should reuse the hot cache."""

    store: dict[str, object] = {}
    build_count = {"vix": 0, "equity": 0}

    monkeypatch.setattr(signals_feed, "cache_get", lambda key: store.get(key))
    monkeypatch.setattr(signals_feed, "cache_set", lambda key, value, ttl=0: store.setdefault(key, value))
    monkeypatch.setattr(signals_feed, "build_default_toolkit", lambda: object())

    def fake_vix(_tk: object) -> signals_feed.SignalCard:
        build_count["vix"] += 1
        return signals_feed.SignalCard(
            id="macro-vix",
            type="macro",
            priority="low",
            tag="VOL",
            time_cn="cached",
            title="VIX",
            ticker="^VIX",
            direction="neut",
            strength=1,
            summary="VIX",
        )

    def fake_equity(_tk: object, ticker: str) -> list[signals_feed.SignalCard]:
        build_count["equity"] += 1
        return [
            signals_feed.SignalCard(
                id=f"eq-{ticker.lower()}",
                type="strategy",
                priority="low",
                tag="Market",
                time_cn="cached",
                title=ticker,
                ticker=ticker,
                direction="neut",
                strength=1,
                summary=ticker,
            )
        ]

    monkeypatch.setattr(signals_feed, "_vix_macro_card", fake_vix)
    monkeypatch.setattr(signals_feed, "_build_equity_cards", fake_equity)

    first = signals_feed.signals_feed(locale="zh", refresh=True, _=None)
    second = signals_feed.signals_feed(locale="zh", _=None)

    assert len(first.signals) == len(second.signals) == 4
    assert build_count == {"vix": 1, "equity": 3}


def test_signals_feed_cold_path_defers_live_build(monkeypatch) -> None:
    """Cold signal feed should not block on live quote/options providers."""

    def fail_toolkit() -> object:
        raise AssertionError("signals_feed should defer live provider work")

    monkeypatch.setattr(signals_feed, "cache_get", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(signals_feed, "build_default_toolkit", fail_toolkit)
    monkeypatch.setattr(signals_feed, "_should_schedule_signals_warm", lambda *_a, **_k: False, raising=False)

    env = signals_feed.signals_feed(locale="zh", _=None)

    assert env.source == "signals_cache_miss_refreshing"
    assert env.signals == []


@pytest.mark.asyncio
async def test_market_insights_cold_path_defers_llm(monkeypatch) -> None:
    """Cold market insights should return fast fallback and warm LLM in background."""

    called = {"generate": False}
    context = {
        "overview": {"pulse": [], "volatility": {}, "liquidity": {}},
        "signals": {"signals": []},
        "treasury": {"rates": []},
    }

    async def fail_if_awaited(*_args: object, **_kwargs: object) -> object:
        called["generate"] = True
        raise AssertionError("generate_mvp_market_insights should be deferred")

    monkeypatch.setattr(mvp_route, "build_mvp_market_context", lambda: context)
    monkeypatch.setattr(mvp_route, "generate_mvp_market_insights", fail_if_awaited)
    monkeypatch.setattr(mvp_route, "has_llm_provider", lambda: True)
    monkeypatch.setattr(mvp_route, "get_cached_mvp_market_insights", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr(mvp_route, "market_insights_cache_key", lambda *_a, **_k: "test-key", raising=False)
    monkeypatch.setattr(mvp_route, "_should_schedule_llm", lambda *_a, **_k: True)

    body = await mvp_route.mvp_market_insights(
        background_tasks=BackgroundTasks(),
        locale="zh",
        entitlement=SimpleNamespace(tier="pro"),
    )

    assert body["tier"] == "pro"
    assert "regime" in body
    assert called["generate"] is False


def test_mvp_market_context_does_not_build_signals_on_cache_miss(monkeypatch) -> None:
    """Market-insights context should not trigger slow live signals on cache miss."""

    def fail_signals(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("build_mvp_market_context should not compute live signals")

    monkeypatch.setattr(mvp_market_context, "cache_get", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mvp_market_context, "get_cached_signals_feed", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr(mvp_market_context, "signals_feed", fail_signals, raising=False)
    monkeypatch.setattr(mvp_market_context, "SessionLocal", lambda: SimpleNamespace(
        execute=lambda *_a, **_k: SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])),
        close=lambda: None,
    ))

    context = mvp_market_context.build_mvp_market_context()

    assert context["signals"]["signals"] == []


def test_market_overview_cold_path_defers_full_rebuild(monkeypatch) -> None:
    """Cold dashboard overview should return a fast fallback and warm in background."""

    def fail_toolkit() -> object:
        raise AssertionError("market_overview should defer the heavy toolkit path")

    monkeypatch.setattr(market_dashboard, "cache_get", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(market_dashboard, "_should_schedule_overview_warm", lambda *_a, **_k: True, raising=False)
    monkeypatch.setattr(market_dashboard, "build_default_toolkit", fail_toolkit)

    body = market_dashboard.market_overview(
        background_tasks=BackgroundTasks(),
        _=None,
        refresh=False,
    )

    assert body["cacheMiss"] is True
    assert body["pulse"] == []


def test_social_radar_route_uses_cache(monkeypatch) -> None:
    store: dict[str, object] = {}
    calls = {"radar": 0}

    monkeypatch.setattr(social_route, "cache_get", lambda key: store.get(key), raising=False)
    monkeypatch.setattr(social_route, "cache_set", lambda key, value, ttl=0: store.setdefault(key, value), raising=False)
    monkeypatch.setattr(social_route, "get_settings", lambda: SimpleNamespace(feature_social_enabled=True))

    def fake_radar(limit: int) -> SimpleNamespace:
        calls["radar"] += 1
        return SimpleNamespace(model_dump=lambda: {"generated_at_utc": "now", "items": []})

    monkeypatch.setattr(social_route, "get_social_radar", fake_radar)

    first = social_route.social_radar(limit=6, _=None)
    second = social_route.social_radar(limit=6, _=None)

    assert first == second
    assert calls == {"radar": 1}


def test_smart_vs_retail_route_uses_cache(monkeypatch) -> None:
    store: dict[str, object] = {}
    calls = {"smart": 0}

    monkeypatch.setattr(social_route, "cache_get", lambda key: store.get(key), raising=False)
    monkeypatch.setattr(social_route, "cache_set", lambda key, value, ttl=0: store.setdefault(key, value), raising=False)
    monkeypatch.setattr(social_route, "get_settings", lambda: SimpleNamespace(feature_social_enabled=True))

    def fake_smart(symbol: str) -> social_route.SmartVsRetailSnapshot:
        calls["smart"] += 1
        return social_route.SmartVsRetailSnapshot(
            symbol=symbol.upper(),
            snapshot_time="now",
            institutional_direction="neutral",
            institutional_strength=0,
            unusual_flow_count_24h=0,
            premium_flow_usd=0,
            retail_direction="neutral",
            retail_sentiment_score=50,
            mentions_24h=0,
            mention_growth_pct=0.0,
            consensus_type="neutral",
            ai_narrative_zh="中文",
            ai_narrative_en="English",
            confidence=0.0,
        )

    monkeypatch.setattr(social_route, "build_smart_vs_retail", fake_smart)

    first = social_route.smart_vs_retail("spy", locale="zh", _=None)
    second = social_route.smart_vs_retail("SPY", locale="zh", _=None)

    assert first == second
    assert calls == {"smart": 1}
