"""Lightweight tests for v2 options analytics helpers."""
from __future__ import annotations

import datetime as dt
import math
from types import SimpleNamespace

from app.analytics.options_chain_analysis import build_chain_analysis
from app.analytics.unusual_v2 import dollar_flow_estimate, score_row, score_snapshot_rows


def _row(**kwargs: object) -> SimpleNamespace:
    base = dict(
        ticker="O:TEST",
        underlying_ticker="SPY",
        contract_type="call",
        expiration_date=dt.date(2026, 6, 20),
        strike_price=500.0,
        bid=4.0,
        ask=4.2,
        midpoint=4.1,
        open_interest=1000,
        day_volume=5000,
        implied_volatility=0.22,
        delta=0.45,
        gamma=0.01,
        theta=-0.05,
        vega=0.12,
        underlying_price=510.0,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_parity_flag_when_mispriced() -> None:
    call = _row(contract_type="call", bid=10.0, ask=10.2, midpoint=10.1, strike_price=500.0)
    put = _row(
        contract_type="put",
        ticker="O:PUT",
        bid=1.0,
        ask=1.2,
        midpoint=1.1,
        strike_price=500.0,
    )
    out = build_chain_analysis(
        symbol="SPY",
        expiration_iso="2026-06-20",
        rows_call=[call],
        rows_put=[put],
        as_of_date=dt.date(2026, 5, 12),
        risk_free_rate=0.04,
    )
    row = next(s for s in out["strikes"] if abs(float(s["strike"]) - 500) < 1e-6)
    assert row["parity"]["flag"] is True


def test_dollar_flow_estimate() -> None:
    r = _row(midpoint=None, bid=2.0, ask=2.4, day_volume=1000, delta=0.5)
    val = dollar_flow_estimate(r)
    mid = (2.0 + 2.4) / 2.0
    assert math.isclose(val, mid * 1000 * 0.5 * 100.0, rel_tol=1e-6)


def test_score_weights_vol_oi() -> None:
    r = _row(day_volume=10000, open_interest=100, implied_volatility=0.5)
    stats = {"2026-06-20": (0.22, 0.02)}
    score, reasons, _ = score_row(r, expiry_stats=stats, update_oi_cache=False)
    assert score >= 40
    assert any("Vol/OI" in x for x in reasons)


def test_score_snapshot_rows_includes_last_trade_at() -> None:
    traded_at = dt.datetime(2026, 5, 20, 14, 30, tzinfo=dt.timezone.utc)
    r = _row(
        day_volume=10000,
        open_interest=100,
        last_trade_at=traded_at,
        last_trade_price=1.25,
        last_trade_size=10,
        snapshot_time=dt.datetime(2026, 5, 20, 15, 0, tzinfo=dt.timezone.utc),
    )
    items = score_snapshot_rows([r], min_score=0)
    assert len(items) == 1
    assert items[0]["lastTradeAt"] == traded_at.isoformat()
    assert items[0]["lastTradePrice"] == 1.25
    assert items[0]["snapshotTime"] is not None
