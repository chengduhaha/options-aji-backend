"""Regression tests for GEX history snapshot seeding."""
from __future__ import annotations

from app.api.routes import options


def test_options_gex_cached_profile_records_history_snapshot(monkeypatch) -> None:
    cached = {
        "symbol": "SPY",
        "netGex": 1.25,
        "gammaFlip": 690.0,
        "underlyingPrice": 695.0,
        "maxPain": 700.0,
        "regime": "Positive Gamma",
    }
    recorded: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(options, "cache_get", lambda _key: cached)
    monkeypatch.setattr(options, "record_gex_snapshot", lambda sym, payload: recorded.append((sym, payload)))
    monkeypatch.setattr(options, "compute_gex_profile", lambda _sym: {"error": "should_not_compute"})

    result = options.get_gex("spy")

    assert result is cached
    assert recorded == [("SPY", cached)]


def test_options_gex_cache_miss_uses_db_snapshot_before_live_compute(monkeypatch) -> None:
    class Row:
        ticker = "SPY260619C00700000"
        underlying_ticker = "SPY"
        contract_type = "call"
        expiration_date = "2026-06-19"
        strike_price = 700.0
        gamma = 0.04
        open_interest = 1000
        implied_volatility = 0.25
        underlying_price = 695.0
        midpoint = 3.2

    class Scalars:
        def all(self):
            return [Row()]

    class Result:
        def scalars(self):
            return Scalars()

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            return Result()

    monkeypatch.setattr(options, "cache_get", lambda _key: None)
    monkeypatch.setattr(options, "cache_set", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(options, "record_gex_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(options, "SessionLocal", lambda: Session())
    monkeypatch.setattr(
        options,
        "compute_gex_profile",
        lambda _sym: (_ for _ in ()).throw(AssertionError("GEX should use DB before live compute")),
    )

    result = options.get_gex("spy")

    assert result["symbol"] == "SPY"
    assert result["source"] == "database_gamma_estimate"
