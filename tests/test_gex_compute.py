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


def test_compute_gex_profile_does_not_short_circuit_on_futu_failure(monkeypatch) -> None:
    import app.analytics.gex_compute as gex_mod
    from app.analytics.gex_compute import compute_gex_profile

    class _BrokenFutu:
        def get_stock_quote(self, _symbol: str) -> dict[str, float]:
            raise RuntimeError("futu down")

        def get_option_chain_snapshot(self, *_args, **_kwargs) -> dict[str, list]:
            raise RuntimeError("futu down")

    monkeypatch.setattr(gex_mod, "get_settings", lambda: type("Cfg", (), {"futu_enabled": True})())
    monkeypatch.setattr(gex_mod, "get_futu_client", lambda: _BrokenFutu())
    monkeypatch.setattr(
        gex_mod,
        "compute_gex_profile_from_contracts",
        lambda *_args, **_kwargs: {"symbol": "AVGO", "netGex": 0.01, "gammaFlip": 400.0},
    )
    monkeypatch.setattr(
        gex_mod,
        "yf_ticker",
        lambda _sym: type(
            "T",
            (),
            {
                "options": ["2026-07-18"],
                "fast_info": {"last_price": 382.0},
                "option_chain": lambda self, _exp: type(
                    "C",
                    (),
                    {
                        "calls": __import__("pandas").DataFrame(
                            {"strike": [380.0], "openInterest": [1000], "impliedVolatility": [0.3]}
                        ),
                        "puts": __import__("pandas").DataFrame(
                            {"strike": [380.0], "openInterest": [800], "impliedVolatility": [0.32]}
                        ),
                    },
                )(),
            },
        )(),
    )
    monkeypatch.setattr(gex_mod, "_resolve_spot", lambda _sym, yf_spot: (yf_spot, "yfinance_fast_info"))

    result = compute_gex_profile("AVGO")

    assert result.get("error") != "futu_gex_failed"
    assert isinstance(result.get("netGex"), (int, float))


def test_vix_term_structure_import():
    from app.analytics.iv_metrics import vix_term_structure_hint

    hint = vix_term_structure_hint()
    assert isinstance(hint, dict)
