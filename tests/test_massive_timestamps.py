"""Tests for Massive sip_timestamp parsing."""
from __future__ import annotations

from datetime import datetime, timezone

from app.utils.massive_timestamps import sip_timestamp_to_datetime


def test_sip_timestamp_nanoseconds() -> None:
    # 2023-02-01 ~ sample from Massive docs
    dt = sip_timestamp_to_datetime(1675280958783136800)
    assert dt is not None
    assert dt.tzinfo == timezone.utc
    assert isinstance(dt, datetime)


def test_sip_timestamp_none() -> None:
    assert sip_timestamp_to_datetime(None) is None
    assert sip_timestamp_to_datetime("") is None
