"""US equity session labels (Eastern Time). Best-effort; does not exchange-holiday fetch."""

from __future__ import annotations

import datetime as dt
from typing import Literal

MarketSession = Literal["pre_market", "regular", "after_hours", "closed"]


def _now_eastern() -> dt.datetime:
    try:
        zone = dt.zoneinfo.ZoneInfo("America/New_York")  # type: ignore[attr-defined]
        return dt.datetime.now(zone)
    except Exception:
        now_utc = dt.datetime.now(dt.timezone.utc)
        return now_utc.astimezone(dt.timezone(dt.timedelta(hours=-5)))


def get_us_market_session(
    now: dt.datetime | None = None,
) -> tuple[MarketSession, str]:
    """Return session enum and Chinese label."""

    t = now or _now_eastern()
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc).astimezone(_now_eastern().tzinfo or dt.timezone.utc)

    t_local = t.astimezone(_now_eastern().tzinfo or dt.timezone.utc)
    wd = t_local.weekday()
    if wd >= 5:
        return "closed", "休市"

    pre_open = t_local.replace(hour=4, minute=0, second=0, microsecond=0)
    reg_open = t_local.replace(hour=9, minute=30, second=0, microsecond=0)
    reg_close = t_local.replace(hour=16, minute=0, second=0, microsecond=0)
    post_close = t_local.replace(hour=20, minute=0, second=0, microsecond=0)

    if t_local < pre_open or t_local >= post_close:
        return "closed", "休市"
    if pre_open <= t_local < reg_open:
        return "pre_market", "盘前"
    if reg_open <= t_local < reg_close:
        return "regular", "开盘"
    if reg_close <= t_local < post_close:
        return "after_hours", "盘后"
    return "closed", "休市"
