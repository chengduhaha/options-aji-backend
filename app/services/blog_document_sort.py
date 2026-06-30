"""Sort keys for member document library PDFs."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from app.db.models_blog import BlogAttachmentRow

_FILENAME_DATE_COMPACT = re.compile(
    r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)"
)
_FILENAME_DATE_SEPARATED = re.compile(
    r"(20\d{2})[-.](0[1-9]|1[0-2])[-.](0[1-9]|[12]\d|3[01])"
)

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def parse_filename_date(filename: str) -> datetime | None:
    """Extract YYYYMMDD (or YYYY-MM-DD / YYYY.MM.DD) from a PDF filename."""
    stem = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
    candidates: list[datetime] = []

    for pattern in (_FILENAME_DATE_COMPACT, _FILENAME_DATE_SEPARATED):
        for match in pattern.finditer(stem):
            year = int(match.group(1))
            month = int(match.group(2))
            day = int(match.group(3))
            try:
                candidates.append(datetime(year, month, day, tzinfo=timezone.utc))
            except ValueError:
                continue

    if not candidates:
        return None
    return max(candidates)


def _normalize_dt(value: datetime | None) -> datetime:
    if value is None:
        return _EPOCH
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def document_sort_key(row: BlogAttachmentRow) -> tuple[bool, datetime, datetime, str]:
    """Newest first: dated filenames, then created_at fallback, then id."""
    filename_date = parse_filename_date(row.original_filename)
    created_at = _normalize_dt(row.created_at)
    has_filename_date = filename_date is not None
    primary_date = filename_date if has_filename_date else created_at
    return (has_filename_date, primary_date, created_at, row.id)


def sort_documents(rows: list[BlogAttachmentRow]) -> list[BlogAttachmentRow]:
    return sorted(rows, key=document_sort_key, reverse=True)
