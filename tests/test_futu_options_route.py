from __future__ import annotations

from types import SimpleNamespace

import pytest


class DbShouldNotBeUsed:
    def execute(self, *args, **kwargs):
        raise AssertionError("DB should not be queried when Futu returns live chain")


def test_options_chain_route_uses_cache_for_non_realtime(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api.routes.options as options_route

    cached = {"symbol": "AAPL", "source": "database", "contracts": [{"ticker": "cached"}]}
    calls = {"futu": 0}

    def forbidden_futu():
        calls["futu"] += 1
        return SimpleNamespace(get_option_chain_snapshot=lambda *args, **kwargs: {"contracts": []})

    monkeypatch.setattr(
        options_route,
        "get_settings",
        lambda: SimpleNamespace(futu_enabled=True, futu_cache_ttl_seconds=3, massive_api_key=""),
    )
    monkeypatch.setattr(
        options_route,
        "get_futu_client",
        forbidden_futu,
        raising=False,
    )
    monkeypatch.setattr(options_route, "cache_get", lambda key: cached)
    monkeypatch.setattr(options_route, "cache_set", lambda *args, **kwargs: None)

    result = options_route.get_options_chain("aapl", db=DbShouldNotBeUsed())

    assert result is cached
    assert calls == {"futu": 0}


def test_options_chain_route_prefers_futu_for_realtime(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api.routes.options as options_route

    monkeypatch.setattr(
        options_route,
        "get_settings",
        lambda: SimpleNamespace(futu_enabled=True, futu_cache_ttl_seconds=3, massive_api_key=""),
    )
    monkeypatch.setattr(
        options_route,
        "cache_get",
        lambda key: {"symbol": "AAPL", "source": "database", "contracts": [{"ticker": "stale"}]},
    )
    monkeypatch.setattr(options_route, "cache_set", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        options_route,
        "get_futu_client",
        lambda: SimpleNamespace(
            get_option_chain_snapshot=lambda *args, **kwargs: {
                "symbol": "AAPL",
                "source": "futu",
                "contracts": [{"ticker": "US.AAPL260619C00195000"}],
                "count": 1,
            }
        ),
        raising=False,
    )

    result = options_route.get_options_chain("aapl", realtime=True, db=DbShouldNotBeUsed())

    assert result["source"] == "futu"
    assert result["contracts"][0]["ticker"] == "US.AAPL260619C00195000"
