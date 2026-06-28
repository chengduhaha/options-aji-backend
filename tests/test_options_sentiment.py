from __future__ import annotations

from app.services.options_leaderboard import get_options_sentiment


def test_get_options_sentiment_aggregates_volume(monkeypatch) -> None:
    sample_items = [
        {"option_type": "C", "underlying": "SPY", "volume": 1000, "strike": 500.0, "expiry": "2026-06-28"},
        {"option_type": "C", "underlying": "NVDA", "volume": 800, "strike": 130.0, "expiry": "2026-06-28"},
        {"option_type": "P", "underlying": "SPY", "volume": 600, "strike": 495.0, "expiry": "2026-06-28"},
        {"option_type": "P", "underlying": "QQQ", "volume": 400, "strike": 480.0, "expiry": "2026-06-28"},
    ]

    def fake_get_leaderboard(board: str, *, force_refresh: bool = False):
        del force_refresh
        assert board == "volume"
        return {"items": sample_items, "updated_at": "2026-06-28T12:00:00Z"}

    monkeypatch.setattr("app.services.options_leaderboard.get_leaderboard", fake_get_leaderboard)

    result = get_options_sentiment()
    assert result["call_volume"] == 1800
    assert result["put_volume"] == 1000
    assert result["put_call_ratio"] == round(1000 / 1800, 4)
    assert len(result["top_calls"]) == 2
    assert result["top_calls"][0]["underlying"] == "SPY"
    assert len(result["top_puts"]) == 2
