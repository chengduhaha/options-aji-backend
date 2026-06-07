from __future__ import annotations

import datetime as dt

import pandas as pd

from app.tools.stock_history import history_last_date, is_history_fresh


def test_history_last_date_from_dataframe() -> None:
    idx = pd.to_datetime(["2026-06-01", "2026-06-05"])
    hist = pd.DataFrame({"Close": [100.0, 101.0]}, index=idx)
    assert history_last_date(hist) == dt.date(2026, 6, 5)


def test_is_history_fresh_rejects_stale() -> None:
    idx = pd.to_datetime(["2025-01-01"])
    hist = pd.DataFrame({"Close": [100.0]}, index=idx)
    assert is_history_fresh(hist, max_stale_days=10) is False
