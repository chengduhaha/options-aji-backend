"""CBOE equity put/call ratio from published daily CSV (no API key)."""

from __future__ import annotations

import csv
import io
import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_CACHE: dict[str, object] = {
    "ts": 0.0,
    "url": "",
    "row": None,
}


@dataclass(frozen=True)
class CboeEquityPcSnapshot:
    trade_date: str
    call_volume: int
    put_volume: int
    total_volume: int
    put_call_ratio: float
    source_url: str


def _parse_float_cell(cell: str) -> float:
    return float(str(cell).strip())


def _parse_int_cell(cell: str) -> int:
    return int(float(str(cell).replace(",", "").strip()))


def fetch_equity_pc_latest(
    *,
    csv_url: str,
    ttl_seconds: float = 3600.0,
) -> CboeEquityPcSnapshot | None:
    """Download CSV and return the last numeric row (previous day’s aggregate in file)."""

    url = csv_url.strip()
    if not url:
        return None

    now = time.monotonic()
    if (
        _CACHE.get("url") == url
        and now - float(_CACHE.get("ts") or 0.0) < ttl_seconds
        and _CACHE.get("row") is not None
    ):
        return _CACHE["row"]  # type: ignore[return-value]

    try:
        with httpx.Client(timeout=45.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            text = resp.text
    except (httpx.HTTPError, OSError, UnicodeDecodeError) as exc:
        logger.warning("CBOE equity P/C fetch failed: %s", exc)
        return None

    lines = text.splitlines()
    header_idx = -1
    for i, line in enumerate(lines):
        if line.strip().upper().startswith("DATE,") and "P/C" in line.upper():
            header_idx = i
            break
    if header_idx < 0:
        logger.warning("CBOE CSV: no header row found")
        return None

    buf = io.StringIO("\n".join(lines[header_idx:]))
    reader = csv.DictReader(buf)
    last_row: dict[str, str] | None = None
    for row in reader:
        if not row:
            continue
        date_val = (row.get("DATE") or "").strip()
        if not date_val:
            continue
        try:
            _parse_int_cell(row.get("CALL") or "0")
            _parse_int_cell(row.get("PUT") or "0")
            _parse_float_cell(row.get("P/C Ratio") or row.get("P/C ratio") or "0")
        except (TypeError, ValueError):
            continue
        last_row = {k.strip(): (v or "").strip() for k, v in row.items() if k}

    if last_row is None:
        return None

    try:
        snap = CboeEquityPcSnapshot(
            trade_date=str(last_row.get("DATE", "")).strip(),
            call_volume=_parse_int_cell(last_row.get("CALL", "0")),
            put_volume=_parse_int_cell(last_row.get("PUT", "0")),
            total_volume=_parse_int_cell(last_row.get("TOTAL", "0")),
            put_call_ratio=_parse_float_cell(last_row.get("P/C Ratio") or "0"),
            source_url=url,
        )
    except (TypeError, ValueError) as exc:
        logger.warning("CBOE CSV parse failed: %s", exc)
        return None

    _CACHE.update({"ts": now, "url": url, "row": snap})
    return snap
