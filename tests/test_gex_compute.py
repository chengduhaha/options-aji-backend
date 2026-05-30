from __future__ import annotations

from app.analytics.gex_compute import _resolve_spot, bs_gamma


def test_bs_gamma_positive():
    g = bs_gamma(spot=100.0, strike=100.0, t_years=0.1, iv=0.25, rate=0.05)
    assert g > 0


def test_resolve_spot_prefers_authoritative_quote(monkeypatch):
    monkeypatch.setattr("app.analytics.gex_compute._quote_spot_from_fmp", lambda _symbol: 120.93)

    assert _resolve_spot("INTC", 100.0) == (120.93, "fmp_quote")


def test_resolve_spot_falls_back_to_yfinance_when_quote_missing(monkeypatch):
    monkeypatch.setattr("app.analytics.gex_compute._quote_spot_from_fmp", lambda _symbol: None)

    assert _resolve_spot("INTC", 100.0) == (100.0, "yfinance_fast_info")


def test_vix_term_structure_import():
    from app.analytics.iv_metrics import vix_term_structure_hint

    hint = vix_term_structure_hint()
    assert isinstance(hint, dict)
