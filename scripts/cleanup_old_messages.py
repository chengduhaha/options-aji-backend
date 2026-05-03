#!/usr/bin/env python3
"""Rolling retention purge for ingest DB (hourly cron)."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402 pylint: disable=C0415
from app.db.session import SessionLocal  # noqa: E402 pylint: disable=C0415
from app.ingest.message_store import cleanup_retention  # noqa: E402 pylint: disable=C0415


def main() -> int:
    settings = get_settings()
    with SessionLocal() as session:
        removed = cleanup_retention(session)

    print(
        f"cleanup_old_messages.py: retention_days={settings.retention_days} deleted_rows≈{removed}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
