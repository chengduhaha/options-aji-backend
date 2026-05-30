from __future__ import annotations

from types import SimpleNamespace

import pytest


class DbShouldNotBeUsed:
    def execute(self, *args, **kwargs):
        raise AssertionError("DB should not be queried when Futu returns live chain")


def test_options_chain_route_prefers_futu_over_stale_cache_and_db(monkeypatch: pytest.MonkeyPatch) -> None:
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

    result = options_route.get_options_chain("aapl", db=DbShouldNotBeUsed())

    assert result["source"] == "futu"
    assert result["contracts"][0]["ticker"] == "US.AAPL260619C00195000"
