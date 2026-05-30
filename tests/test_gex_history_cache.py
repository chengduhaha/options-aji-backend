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
