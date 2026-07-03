"""Regression tests for GEX history snapshot seeding."""
from __future__ import annotations

import json
import math

from app.analytics import gex_history
from app.api.routes import options
from app.services.membership import V3Access

FREE_ACCESS = V3Access(
    tier="free",
    is_member=False,
    is_full_member=False,
    is_trial_member=False,
    membership_kind=None,
    membership_expires_at=None,
    days_remaining=None,
)


def test_seed_price_closes_skips_nan_values(monkeypatch) -> None:
    import datetime as dt

    class Index:
        def date(self):
            return dt.date(2026, 6, 26)

    class Row:
        def get(self, key):
            return float("nan") if key == "Close" else None

    class Hist:
        empty = False

        def iterrows(self):
            return [(Index(), Row())]

    class Ticker:
        def history(self, **_kwargs):
            return Hist()

    monkeypatch.setattr(gex_history, "yf_ticker", lambda _sym: Ticker())
    assert gex_history.seed_price_closes("SPY") == []


def test_list_gex_history_skips_non_finite_points(monkeypatch) -> None:
    class Redis:
        def hgetall(self, _key):
            return {
                "2026-06-25": json.dumps(
                    {"date": "2026-06-25", "netGex": 1.2, "gammaFlip": float("nan")}
                ),
                "2026-06-26": json.dumps(
                    {"date": "2026-06-26", "netGex": 1.5, "gammaFlip": 690.0}
                ),
            }

    monkeypatch.setattr(gex_history, "redis_client_optional", lambda: Redis())
    rows = gex_history.list_gex_history("SPY")
    assert len(rows) == 2
    assert rows[0]["gammaFlip"] is None
    assert rows[1]["gammaFlip"] == 690.0
    assert all(
        value is None or (isinstance(value, float) and math.isfinite(value))
        for row in rows
        for value in row.values()
        if isinstance(value, float)
    )


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

    result = options.get_gex("spy", access=FREE_ACCESS)

    assert result is cached
    assert recorded == [("SPY", cached)]


def test_options_gex_cache_miss_prefers_futu_before_db(monkeypatch) -> None:
    futu_result = {
        "symbol": "SPY",
        "netGex": 2.5,
        "gammaFlip": 700.0,
        "source": "futu_realtime_gamma_estimate",
        "spotSource": "futu_quote",
        "contractCount": 120,
    }

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

        def scalar_one_or_none(self):
            return 695.0

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
        "_fetch_gex_futu_live",
        lambda *_args, **_kwargs: futu_result,
    )
    monkeypatch.setattr(
        options,
        "compute_gex_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("GEX should use Futu before DB")),
    )

    result = options.get_gex("spy", access=FREE_ACCESS)

    assert result["source"] == "futu_realtime_gamma_estimate"
    assert result["netGex"] == 2.5


def test_options_gex_cache_miss_uses_db_when_futu_unavailable(monkeypatch) -> None:
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

        def scalar_one_or_none(self):
            return 695.0

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
    monkeypatch.setattr(options, "_fetch_gex_futu_live", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        options,
        "compute_gex_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("GEX should use DB before yfinance")),
    )

    result = options.get_gex("spy", access=FREE_ACCESS)

    assert result["symbol"] == "SPY"
    assert result["source"] == "database_gamma_estimate"
