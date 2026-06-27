from __future__ import annotations

import pandas as pd


class FakeOptionScreenContext:
    def __init__(self) -> None:
        self.closed = False
        self.last_request = None

    def close(self) -> None:
        self.closed = True

    def get_option_screen(self, request):
        self.last_request = request
        frame = pd.DataFrame(
            [
                {
                    "code": "US.SPY260626C00729000",
                    "option_name": "SPY 260626 729.00C",
                    "strike_price": 729.0,
                    "strike_date": "2026-06-26",
                    "option_type": 1,
                    "left_day": 0,
                    "volume": 73320,
                    "open_interest": 2544,
                    "vol_oi_ratio": 28.82,
                    "premium": 4.61,
                    "price": 4.61,
                    "turnover": 338005.2,
                    "implied_volatility": 26.6,
                    "history_volatility": 18.2,
                    "iv_hv_ratio": 1.46,
                    "delta": 0.786,
                    "gamma": 0.042,
                    "vega": 0.12,
                    "theta": -0.31,
                    "change_ratio": -0.302,
                    "in_the_money": True,
                    "bid_ask_spread": 0.05,
                    "bid_volume": 120,
                    "ask_volume": 95,
                    "sell_annualized_return": 0.42,
                    "sell_profit_probability": 0.68,
                    "itm_probability": 0.32,
                },
                {
                    "code": "US.NVDA260626C00195000",
                    "option_name": "NVDA 260626 195.00C",
                    "strike_price": 195.0,
                    "strike_date": "2026-06-26",
                    "option_type": 1,
                    "left_day": 0,
                    "volume": 56735,
                    "open_interest": 15225,
                    "vol_oi_ratio": 3.73,
                    "premium": 0.61,
                    "price": 0.61,
                    "turnover": 34608.35,
                    "implied_volatility": 57.1,
                    "history_volatility": 44.0,
                    "iv_hv_ratio": 1.3,
                    "delta": 0.334,
                    "gamma": 0.021,
                    "vega": 0.08,
                    "theta": -0.18,
                    "change_ratio": -0.712,
                    "in_the_money": False,
                    "bid_ask_spread": 0.02,
                    "bid_volume": 80,
                    "ask_volume": 70,
                    "sell_annualized_return": 0.28,
                    "sell_profit_probability": 0.71,
                    "itm_probability": 0.29,
                },
            ]
        )
        return 0, (True, 1_919_926, frame)


def test_get_option_screen_leaderboard_maps_rows() -> None:
    from app.clients.futu_client import FutuQuoteClient

    fake_ctx = FakeOptionScreenContext()
    client = FutuQuoteClient(enabled=True, ctx_factory=lambda: fake_ctx)

    result = client.get_option_screen_leaderboard(vol_oi_min=3.0, volume_min=500, limit=100)

    assert len(result["contracts"]) == 2
    first = result["contracts"][0]
    assert first["underlying"] == "SPY"
    assert first["option_type"] == "C"
    assert first["vol_oi_ratio"] == 28.82
    assert first["change_ratio"] == -30.2
    assert first["moneyness"] in {"ITM", "OTM", "ATM"}
    assert first["turnover"] is not None
    assert fake_ctx.last_request is not None
    assert fake_ctx.last_request.page_count == 100


def test_get_option_screen_board_volume_sort() -> None:
    from app.clients.futu_client import FutuQuoteClient

    fake_ctx = FakeOptionScreenContext()
    client = FutuQuoteClient(enabled=True, ctx_factory=lambda: fake_ctx)
    result = client.get_option_screen_board(sort_indicator="VOLUME", sort_desc=True, limit=50)
    assert len(result["items"]) == 2
    assert fake_ctx.last_request is not None


def test_unusual_leaderboard_pagination(monkeypatch) -> None:
    from app.services import options_leaderboard as svc

    sample_items = [{"rank": i, "code": f"C{i}"} for i in range(1, 101)]
    sample = {
        "board": "unusual",
        "items": sample_items,
        "total": 100,
        "universe_count": 5000,
        "synced_at": "2026-06-26T14:12:00+00:00",
    }
    monkeypatch.setattr(svc, "cache_get", lambda _key: sample)

    page1 = svc.get_unusual_leaderboard_page(page=1, page_size=10)
    page10 = svc.get_unusual_leaderboard_page(page=10, page_size=10)

    assert len(page1["contracts"]) == 10
    assert page1["contracts"][0]["rank"] == 1
    assert page1["page"] == 1
    assert page1["total_pages"] == 10
    assert len(page10["contracts"]) == 10
    assert page10["contracts"][0]["rank"] == 91


def test_get_leaderboard_returns_full_cache(monkeypatch) -> None:
    from app.services import options_leaderboard as svc

    sample = {
        "board": "volume",
        "items": [{"rank": 1, "code": "X"}],
        "total": 1,
        "synced_at": "2026-06-26T14:12:00+00:00",
    }
    monkeypatch.setattr(svc, "cache_get", lambda _key: sample)
    payload = svc.get_leaderboard("volume")
    assert payload["total"] == 1
    assert payload["cache_ttl_seconds"] == 900
    assert payload["items"][0]["code"] == "X"


def test_leaderboard_routes_are_registered() -> None:
    from app.main import create_application

    paths = {route.path for route in create_application().routes}
    assert "/api/options/unusual-leaderboard" in paths
    assert "/api/options/leaderboard/{board}" in paths


def test_resolve_option_strike_prefers_option_name_over_price_field() -> None:
    from app.clients.futu_client import _resolve_option_strike

    row = {
        "code": "US.QQQ260717C711000",
        "option_name": "QQQ 260717 711.00C",
        "strike_price": 17.68,
        "price": 17.68,
        "premium": 17.9,
    }
    assert _resolve_option_strike(row) == 711.0


def test_resolve_option_strike_parses_short_occ_code() -> None:
    from app.clients.futu_client import _resolve_option_strike

    row = {
        "code": "US.HIVE260731C500",
        "option_name": "HIVE 260731 0.50C",
        "strike_price": 0.5,
        "price": 3.6,
    }
    assert _resolve_option_strike(row) == 0.5


def test_map_option_screen_row_sets_strike_not_price() -> None:
    from app.clients.futu_client import FutuQuoteClient

    client = FutuQuoteClient(enabled=True, ctx_factory=lambda: None)
    mapped = client._map_option_screen_row(
        {
            "code": "US.QQQ260717C711000",
            "option_name": "QQQ 260717 711.00C",
            "strike_price": 711.0,
            "strike_date": "2026-07-17",
            "option_type": 1,
            "left_day": 20,
            "volume": 1000,
            "open_interest": 500,
            "price": 17.68,
            "premium": 17.9,
            "underlying": {"code": "US.QQQ", "price": 706.52},
        },
        rank=1,
    )
    assert mapped is not None
    assert mapped["strike"] == 711.0
    assert mapped["strike_price"] == 711.0
    assert mapped["price"] == 17.68
