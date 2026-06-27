from __future__ import annotations

import pandas as pd


class FakeQuoteContext:
    def __init__(self) -> None:
        self.closed = False
        self.snapshot_calls: list[list[str]] = []
        self.option_chain_calls = 0

    def close(self) -> None:
        self.closed = True

    def get_market_snapshot(self, code_list: list[str]):
        self.snapshot_calls.append(code_list)
        rows = []
        for code in code_list:
            if code == "US.AAPL":
                rows.append(
                    {
                        "code": "US.AAPL",
                        "update_time": "2026-05-26 09:35:00",
                        "last_price": 195.25,
                        "open_price": 193.0,
                        "high_price": 196.0,
                        "low_price": 192.5,
                        "prev_close_price": 192.75,
                        "volume": 1234567,
                        "total_market_val": 3010000000000,
                        "pe_ratio": 31.5,
                        "earning_per_share": 6.2,
                    }
                )
            else:
                rows.append(
                    {
                        "code": code,
                        "stock_owner": "US.AAPL",
                        "option_type": "CALL" if code.endswith("C00195000") else "PUT",
                        "strike_time": "2026-06-19",
                        "option_strike_price": 195.0,
                        "option_delta": 0.52,
                        "option_gamma": 0.04,
                        "option_theta": -0.08,
                        "option_vega": 0.17,
                        "option_implied_volatility": 24.5,
                        "option_open_interest": 1200,
                        "bid_price": 4.1,
                        "ask_price": 4.3,
                        "bid_vol": 40,
                        "ask_vol": 35,
                        "last_price": 4.2,
                        "volume": 920,
                        "prev_close_price": 3.9,
                    }
                )
        return 0, pd.DataFrame(rows)

    def get_option_chain(self, code: str, **kwargs):
        self.option_chain_calls += 1
        assert code == "US.AAPL"
        assert kwargs["start"] == "2026-06-19"
        assert kwargs["end"] == "2026-06-19"
        return 0, pd.DataFrame(
            [
                {
                    "code": "US.AAPL260619C00195000",
                    "option_type": "CALL",
                    "stock_owner": "US.AAPL",
                    "strike_time": "2026-06-19",
                    "strike_price": 195.0,
                },
                {
                    "code": "US.AAPL260619P00195000",
                    "option_type": "PUT",
                    "stock_owner": "US.AAPL",
                    "strike_time": "2026-06-19",
                    "strike_price": 195.0,
                },
            ]
        )


def test_normalize_futu_us_code() -> None:
    from app.clients.futu_client import normalize_futu_us_code

    assert normalize_futu_us_code("AAPL") == "US.AAPL"
    assert normalize_futu_us_code("us.aapl") == "US.AAPL"
    assert normalize_futu_us_code("^DJI") == "US..DJI"
    assert normalize_futu_us_code("SPX") == "US..SPX"


def test_get_stock_quote_maps_futu_snapshot() -> None:
    from app.clients.futu_client import FutuQuoteClient

    fake_ctx = FakeQuoteContext()
    client = FutuQuoteClient(enabled=True, ctx_factory=lambda: fake_ctx)

    quote = client.get_stock_quote("AAPL")

    assert quote["source"] == "futu"
    assert quote["symbol"] == "AAPL"
    assert quote["last_price"] == 195.25
    assert quote["previous_close"] == 192.75
    assert quote["change_pct"] == 1.297
    assert quote["volume"] == 1234567
    assert quote["market_cap"] == 3010000000000
    assert quote["pe"] == 31.5


def test_get_stock_quotes_batches_symbols() -> None:
    from app.clients.futu_client import FutuQuoteClient

    fake_ctx = FakeQuoteContext()
    client = FutuQuoteClient(enabled=True, snapshot_batch_size=2, ctx_factory=lambda: fake_ctx)

    quotes = client.get_stock_quotes(["AAPL"])

    assert quotes[0]["symbol"] == "AAPL"
    assert quotes[0]["source"] == "futu"
    assert fake_ctx.snapshot_calls == [["US.AAPL"]]


def test_get_option_chain_snapshot_merges_static_and_realtime_rows() -> None:
    from app.clients.futu_client import FutuQuoteClient

    fake_ctx = FakeQuoteContext()
    client = FutuQuoteClient(enabled=True, ctx_factory=lambda: fake_ctx)

    result = client.get_option_chain_snapshot("AAPL", expiration_date="2026-06-19", limit=10)

    assert result["source"] == "futu"
    assert result["symbol"] == "AAPL"
    assert result["count"] == 2
    first = result["contracts"][0]
    assert first["ticker"] == "US.AAPL260619C00195000"
    assert first["underlying"] == "AAPL"
    assert first["contract_type"] == "call"
    assert first["expiration_date"] == "2026-06-19"
    assert first["strike_price"] == 195.0
    assert first["bid"] == 4.1
    assert first["ask"] == 4.3
    assert first["midpoint"] == 4.2
    assert first["implied_volatility"] == 0.245
    assert first["day_volume"] == 920
    assert first["open_interest"] == 1200


def test_get_option_chain_snapshot_uses_short_ttl_cache() -> None:
    import app.clients.futu_client as futu_mod
    from app.clients.futu_client import FutuQuoteClient

    futu_mod._chain_snapshot_cache.clear()
    fake_ctx = FakeQuoteContext()
    client = FutuQuoteClient(enabled=True, ctx_factory=lambda: fake_ctx)

    first = client.get_option_chain_snapshot("AAPL", expiration_date="2026-06-19", limit=10)
    second = client.get_option_chain_snapshot("AAPL", expiration_date="2026-06-19", limit=10)

    assert first is second
    assert fake_ctx.option_chain_calls == 1
    assert len(fake_ctx.snapshot_calls) == 1


def test_disabled_futu_client_returns_not_enabled() -> None:
    from app.clients.futu_client import FutuQuoteClient

    client = FutuQuoteClient(enabled=False, ctx_factory=FakeQuoteContext)

    assert client.get_stock_quote("AAPL") == {"symbol": "AAPL", "error": "futu_not_enabled"}
    assert client.get_option_chain_snapshot("AAPL") == {
        "symbol": "AAPL",
        "contracts": [],
        "error": "futu_not_enabled",
    }


def test_unreachable_opend_returns_fast_error() -> None:
    from app.clients.futu_client import FutuQuoteClient
    from app.clients.futu_pool import FutuConnectionPool, reset_futu_pool_for_tests

    reset_futu_pool_for_tests()
    pool = FutuConnectionPool(
        host="127.0.0.1",
        port=1,
        min_size=0,
        max_size=2,
        acquire_timeout_sec=0.2,
        connect_timeout_seconds=0.05,
    )
    client = FutuQuoteClient(
        enabled=True,
        host="127.0.0.1",
        port=1,
        connect_timeout_seconds=0.05,
        ctx_factory=None,
    )

    # Inject unreachable pool for this host/port without warming global production pool.
    import app.clients.futu_pool as pool_mod

    pool_mod._pools[("127.0.0.1", 1)] = pool

    quote = client.get_stock_quote("AAPL")

    assert quote["symbol"] == "AAPL"
    assert "futu_opend_unreachable" in quote["error"] or "futu_pool" in quote["error"]
    reset_futu_pool_for_tests()
