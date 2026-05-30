"""Parse Massive API nanosecond timestamps."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def sip_timestamp_to_datetime(raw: object) -> Optional[datetime]:
    """Convert Massive sip_timestamp (nanoseconds since epoch) to UTC datetime."""
    if raw is None:
        return None
    try:
        if isinstance(raw, str):
            ns = int(raw.strip())
        elif isinstance(raw, (int, float)):
            ns = int(raw)
        else:
            return None
        if ns <= 0:
            return None
        # Massive uses nanoseconds; values < 1e12 are likely already in seconds/ms
        if ns < 1_000_000_000_000:
            if ns < 1_000_000_000:
                sec = float(ns)
            else:
                sec = ns / 1000.0
        else:
            sec = ns / 1_000_000_000.0
        return datetime.fromtimestamp(sec, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
