"""Purge old rows from options_snapshots to cap table growth."""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import delete, or_

from app.config import get_settings
from app.db.models import OptionsSnapshotRow
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)


def purge_options_snapshots() -> int:
    """Delete expired contracts and stale snapshots. Returns total rows removed."""

    cfg = get_settings()
    expired_days = max(1, int(cfg.options_snapshot_expired_purge_days))
    stale_days = max(1, int(cfg.options_snapshot_stale_purge_days))

    today = dt.date.today()
    expired_cutoff = today - dt.timedelta(days=expired_days)
    stale_cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=stale_days)

    session = SessionLocal()
    try:
        stmt = delete(OptionsSnapshotRow).where(
            or_(
                OptionsSnapshotRow.expiration_date < expired_cutoff,
                OptionsSnapshotRow.snapshot_time < stale_cutoff,
            )
        )
        result = session.execute(stmt)
        session.commit()
        removed = int(result.rowcount or 0)
        logger.info(
            "options_snapshots retention purge: removed=%s expired_before=%s stale_before=%s",
            removed,
            expired_cutoff.isoformat(),
            stale_cutoff.isoformat(),
        )
        return removed
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
