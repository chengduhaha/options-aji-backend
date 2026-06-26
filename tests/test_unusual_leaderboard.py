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
                    "implied_volatility": 26.6,
                    "delta": 0.786,
                    "change_ratio": -0.302,
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
                    "implied_volatility": 57.1,
                    "delta": 0.334,
                    "change_ratio": -0.712,
                },
            ]
        )
        return 0, (True, 1_919_926, frame)


def test_get_option_screen_leaderboard_maps_rows() -> None:
    from app.clients.futu_client import FutuQuoteClient

    fake_ctx = FakeOptionScreenContext()
    client = FutuQuoteClient(enabled=True, ctx_factory=lambda: fake_ctx)

    result = client.get_option_screen_leaderboard(vol_oi_min=3.0, volume_min=500, limit=100)

    assert result["source"] == "futu"
    assert result["universe_count"] == 1_919_926
    assert len(result["contracts"]) == 2
    first = result["contracts"][0]
    assert first["underlying"] == "SPY"
    assert first["option_type"] == "C"
    assert first["vol_oi_ratio"] == 28.82
    assert first["change_ratio"] == -30.2
    assert fake_ctx.last_request is not None
    assert fake_ctx.last_request.page_count == 100


def test_unusual_leaderboard_pagination(monkeypatch) -> None:
    from app.services import unusual_leaderboard as svc

    sample = {
        "contracts": [{"rank": i, "code": f"C{i}"} for i in range(1, 101)],
        "total": 100,
        "universe_count": 5000,
        "source": "futu",
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


def test_unusual_leaderboard_route_is_registered() -> None:
    from app.main import create_application

    paths = {route.path for route in create_application().routes}
    assert "/api/options/unusual-leaderboard" in paths
