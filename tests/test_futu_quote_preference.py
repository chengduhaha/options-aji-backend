from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_openbb_quote_prefers_futu_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.tools.openbb_tools as openbb_tools

    monkeypatch.setattr(
        openbb_tools,
        "get_settings",
        lambda: SimpleNamespace(futu_enabled=True, fmp_api_key="should-not-be-used"),
    )
    monkeypatch.setattr(
        openbb_tools,
        "get_futu_client",
        lambda: SimpleNamespace(
            get_stock_quote=lambda symbol: {
                "symbol": symbol.upper(),
                "source": "futu",
                "last_price": 201.0,
                "previous_close": 200.0,
                "change_pct": 0.5,
                "volume": 1000,
            }
        ),
        raising=False,
    )
    monkeypatch.setattr(
        openbb_tools,
        "get_fmp_client",
        lambda: pytest.fail("FMP should not be called when Futu quote succeeds"),
    )

    quote = openbb_tools.OpenBBToolkit().get_quote("aapl")

    assert quote["source"] == "futu"
    assert quote["last_price"] == 201.0


def test_openbb_quote_falls_back_when_futu_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.tools.openbb_tools as openbb_tools

    monkeypatch.setattr(
        openbb_tools,
        "get_settings",
        lambda: SimpleNamespace(futu_enabled=True, fmp_api_key="configured"),
    )
    monkeypatch.setattr(
        openbb_tools,
        "get_futu_client",
        lambda: SimpleNamespace(get_stock_quote=lambda symbol: {"symbol": symbol, "error": "futu_down"}),
        raising=False,
    )
    monkeypatch.setattr(
        openbb_tools,
        "get_fmp_client",
        lambda: SimpleNamespace(
            get_quote=lambda symbol: {
                "symbol": symbol,
                "price": 199.0,
                "previousClose": 198.0,
                "changesPercentage": 0.505,
                "volume": 900,
            }
        ),
    )

    quote = openbb_tools.OpenBBToolkit().get_quote("aapl")

    assert quote["symbol"] == "AAPL"
    assert quote["last_price"] == 199.0


def test_openbb_option_chain_full_prefers_futu(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.tools.openbb_tools as openbb_tools

    monkeypatch.setattr(
        openbb_tools,
        "get_settings",
        lambda: SimpleNamespace(futu_enabled=True, fmp_api_key=""),
    )
    monkeypatch.setattr(
        openbb_tools,
        "get_futu_client",
        lambda: SimpleNamespace(
            get_option_chain_snapshot=lambda *args, **kwargs: {
                "symbol": "AAPL",
                "source": "futu",
                "expirations": ["2026-06-19"],
                "contracts": [
                    {
                        "ticker": "US.AAPL260619C00195000",
                        "contract_type": "call",
                        "expiration_date": "2026-06-19",
                        "strike_price": 195.0,
                        "last_trade_price": 4.2,
                        "bid": 4.1,
                        "ask": 4.3,
                        "day_volume": 920,
                        "open_interest": 1200,
                        "implied_volatility": 0.245,
                        "delta": 0.52,
                        "gamma": 0.04,
                    }
                ],
            }
        ),
        raising=False,
    )

    chain = openbb_tools.OpenBBToolkit().get_option_chain_full("aapl")

    assert chain["source"] == "futu"
    assert chain["expiration"] == "2026-06-19"
    assert chain["calls"][0]["contractSymbol"] == "US.AAPL260619C00195000"
    assert chain["calls"][0]["strike"] == 195.0
