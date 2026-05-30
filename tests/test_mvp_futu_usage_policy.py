from __future__ import annotations

from types import SimpleNamespace

import pytest


class DummyDb:
    def execute(self, *args, **kwargs):
        raise AssertionError("DB fallback should not be used when Futu realtime chain succeeds")


def test_options_chain_realtime_bounds_from_futu_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api.routes.options as options_route

    calls: list[dict[str, object]] = []

    class FakeFutu:
        def get_stock_quote(self, symbol: str) -> dict[str, object]:
            assert symbol == "AAPL"
            return {"symbol": "AAPL", "last_price": 200.0, "source": "futu"}

        def get_option_chain_snapshot(self, *args, **kwargs):
            calls.append(dict(kwargs))
            return {
                "symbol": "AAPL",
                "source": "futu",
                "contracts": [{"ticker": "US.AAPL260619C00200000"}],
                "count": 1,
            }

    monkeypatch.setattr(
        options_route,
        "get_settings",
        lambda: SimpleNamespace(futu_enabled=True, futu_cache_ttl_seconds=3, massive_api_key=""),
    )
    monkeypatch.setattr(options_route, "get_futu_client", lambda: FakeFutu(), raising=False)
    monkeypatch.setattr(options_route, "cache_get", lambda key: None)
    monkeypatch.setattr(options_route, "cache_set", lambda *args, **kwargs: None)

    result = options_route.get_options_chain(
        "aapl",
        realtime=True,
        strike_window_pct=0.15,
        limit=400,
        db=DummyDb(),
    )

    assert result["source"] == "futu"
    assert calls == [
        {
            "expiration_date": None,
            "contract_type": None,
            "strike_price_gte": 170.0,
            "strike_price_lte": 230.0,
            "limit": 400,
        }
    ]


def test_compute_gex_profile_from_realtime_contracts() -> None:
    from app.analytics.gex_compute import compute_gex_profile_from_contracts

    result = compute_gex_profile_from_contracts(
        "AAPL",
        contracts=[
            {
                "contract_type": "call",
                "expiration_date": "2026-06-19",
                "strike_price": 195,
                "gamma": 0.04,
                "open_interest": 1200,
                "implied_volatility": 0.24,
            },
            {
                "contract_type": "put",
                "expiration_date": "2026-06-19",
                "strike_price": 190,
                "gamma": 0.05,
                "open_interest": 1500,
                "implied_volatility": 0.28,
            },
        ],
        spot=200.0,
    )

    assert result["symbol"] == "AAPL"
    assert result["source"] == "futu_realtime_gamma_estimate"
    assert result["underlyingPrice"] == 200.0
    assert result["callWall"] == 195.0
    assert result["putWall"] == 190.0
    assert result["netGex"] < 0
    assert result["strikes"]


def test_market_overview_unusual_scan_reads_db_not_live_toolkit(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api.routes.market_dashboard as market_dashboard

    class ForbiddenToolkit:
        def get_option_chain_full(self, *args, **kwargs):
            raise AssertionError("overview must not trigger live option chain scans")

    monkeypatch.setattr(market_dashboard, "SessionLocal", lambda: None, raising=False)
    monkeypatch.setattr(market_dashboard, "_scan_unusual_top_from_db", lambda *, limit: [])

    assert market_dashboard._scan_unusual_top(ForbiddenToolkit(), limit=5) == []


def test_options_chain_sync_skips_heavy_futu_background_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.sync.pipelines.options_chain_sync as options_sync

    monkeypatch.setattr(
        options_sync,
        "get_settings",
        lambda: SimpleNamespace(
            futu_enabled=True,
            futu_background_options_sync_enabled=False,
            massive_api_key="",
            sync_watchlist_symbols=["AAPL"],
        ),
    )
    monkeypatch.setattr(
        options_sync,
        "get_futu_client",
        lambda: (_ for _ in ()).throw(AssertionError("background sync must not open Futu")),
    )

    options_sync.sync_options_chain_pipeline()
