"""Economic calendar snippets (FMP) for unified feed."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import time

from app.clients.fmp_client import get_fmp_client
from app.config import get_settings

logger = logging.getLogger(__name__)

_CACHE: dict[str, object] = {"t": 0.0, "rows": []}


def fetch_macro_calendar_rows(
    *,
    limit: int = 40,
) -> list[dict[str, object]]:
    cfg = get_settings()
    key = cfg.fmp_api_key.strip()
    if not key:
        return []

    now = time.monotonic()
    ttl = max(60.0, float(cfg.macro_feed_cache_seconds))
    if now - float(_CACHE["t"]) < ttl and _CACHE.get("rows"):
        return _CACHE["rows"]  # type: ignore[return-value]

    today = dt.date.today()
    end = today + dt.timedelta(days=16)
    try:
        client = get_fmp_client()
        data = client.get_economic_calendar(today.isoformat(), end.isoformat())
    except Exception as exc:
        logger.warning("FMP economic_calendar failed: %s", exc)
        return []

    if not isinstance(data, list):
        return []

    rows: list[dict[str, object]] = []
    for row in data:
        if isinstance(row, dict):
            rows.append(row)
    rows.sort(key=lambda r: str(r.get("date") or ""))
    trimmed = rows[: max(1, limit)]
    _CACHE.update({"t": now, "rows": trimmed})
    return trimmed


def macro_row_stable_id(row: dict[str, object]) -> str:
    date = str(row.get("date") or "")
    event = str(row.get("event") or "")
    country = str(row.get("country") or "")
    digest = hashlib.sha256(f"{date}|{country}|{event}".encode("utf-8")).hexdigest()[:14]
    return f"macro-{digest}"


def macro_row_timestamp_iso(row: dict[str, object]) -> str:
    date = str(row.get("date") or "")[:10]
    if len(date) == 10:
        return f"{date}T13:00:00+00:00"
    return dt.datetime.now(dt.timezone.utc).isoformat()
