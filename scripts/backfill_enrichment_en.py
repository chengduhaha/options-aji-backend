#!/usr/bin/env python3
"""Backfill English enrichment fields for recent Discord messages."""

from __future__ import annotations

import argparse
import logging
import time

from sqlalchemy import select

from app.db.models import DiscordMessageRow, MessageEnrichmentRow
from app.db.session import SessionLocal
from app.ingest.feed_enrichment import _call_openrouter_enrich
from app.config import get_settings

logger = logging.getLogger(__name__)


def backfill(*, limit: int, sleep_seconds: float) -> int:
    cfg = get_settings()
    done = 0
    session = SessionLocal()
    try:
        rows = list(
            session.scalars(
                select(MessageEnrichmentRow)
                .where(MessageEnrichmentRow.title_en.is_(None))
                .order_by(MessageEnrichmentRow.created_at.desc())
                .limit(limit)
            ).all()
        )
    finally:
        session.close()

    for enr in rows:
        sess = SessionLocal()
        try:
            dm = sess.get(DiscordMessageRow, enr.message_id)
            if dm is None or not (dm.content or "").strip():
                continue
            parsed = _call_openrouter_enrich(cfg, plaintext=dm.content or "", author=dm.author)
            if parsed is None:
                continue
            row = sess.get(MessageEnrichmentRow, enr.message_id)
            if row is None:
                continue
            row.title_en = (parsed.title_en or "")[:512] or None
            row.summary_en = parsed.summary_en or None
            row.bullets_en = [str(b).strip() for b in parsed.bullets_en if str(b).strip()][:5]
            row.risk_note_en = parsed.risk_note_en
            row.enrichment_version = 2
            sess.commit()
            done += 1
            logger.info("backfilled message_id=%s", enr.message_id)
        except Exception:
            logger.exception("backfill failed message_id=%s", enr.message_id)
            sess.rollback()
        finally:
            sess.close()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return done


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Backfill message_enrichment English fields")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--sleep", type=float, default=1.0)
    args = parser.parse_args()
    count = backfill(limit=max(1, args.limit), sleep_seconds=max(0.0, args.sleep))
    print(f"backfilled={count}")


if __name__ == "__main__":
    main()
