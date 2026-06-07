"""Tests for cache-first agent context assembly."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from app.services import agent_context_service as svc


def test_build_agent_context_uses_redis_only(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        svc,
        "cache_get",
        lambda key: {
            "stock:overview:SPY": {
                "bar": {"price": 500.0},
                "keyStats": {"atmIv": 18.5, "ivRank": 42},
                "optionLiquidity": {"pcrVolume": 0.9},
                "expectedMoves": [{"dte": 7, "movePct": 2.1}],
            },
            "gex:SPY": {
                "netGex": 1.2,
                "regime": "positive",
                "gammaFlip": 495.0,
                "maxPain": 500.0,
                "callWall": 510.0,
                "putWall": 490.0,
                "underlyingPrice": 500.0,
                "strikes": [{"strike": 500}],
            },
            "news:stock:SPY": {
                "articles": [
                    {"title": "SPY rises", "source": "test", "published_at": "2026-01-01"}
                ]
            },
            "analyst:ratings:SPY": {
                "ratings": [{"firm": "Goldman", "action": "upgrade", "to": "Buy"}]
            },
            "options:chain:SPY": {
                "contracts": [
                    {
                        "contract_type": "call",
                        "strike_price": 500,
                        "bid": 5.0,
                        "ask": 5.2,
                        "implied_volatility": 0.18,
                        "delta": 0.5,
                        "open_interest": 1000,
                        "day_volume": 500,
                    }
                ]
            },
        }.get(key),
    )

    monkeypatch.setattr(svc, "SessionLocal", lambda: MagicMock())

    ctx = svc.build_agent_context_from_cache("SPY")

    assert ctx["symbol"] == "SPY"
    assert ctx["data_sources"]["gex"] == "redis"
    assert ctx["data_sources"]["overview"] == "redis"
    assert isinstance(ctx.get("gex"), dict)
    assert isinstance(ctx.get("recent_news"), list)


def test_build_agent_context_empty_cache(monkeypatch: Any) -> None:
    monkeypatch.setattr(svc, "cache_get", lambda _key: None)

    class _EmptySession:
        def __enter__(self) -> "_EmptySession":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, *_args: object, **_kwargs: object) -> MagicMock:
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

    monkeypatch.setattr(svc, "SessionLocal", lambda: _EmptySession())

    ctx = svc.build_agent_context_from_cache("SPY")
    assert ctx["symbol"] == "SPY"
    assert ctx["data_sources"] == {}
    assert "缓存未命中" in str(ctx.get("cache_note"))
