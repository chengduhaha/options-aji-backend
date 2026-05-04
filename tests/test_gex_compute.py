from __future__ import annotations

from app.analytics.gex_compute import bs_gamma


def test_bs_gamma_positive():
    g = bs_gamma(spot=100.0, strike=100.0, t_years=0.1, iv=0.25, rate=0.05)
    assert g > 0


def test_vix_term_structure_import():
    from app.analytics.iv_metrics import vix_term_structure_hint

    hint = vix_term_structure_hint()
    assert isinstance(hint, dict)
