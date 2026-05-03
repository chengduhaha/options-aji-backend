"""Persist and query discord messages."""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import Select, delete, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import DiscordMessageRow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredDiscordMessage:
    id: str
    channel_id: str
    author: Optional[str]
    content: Optional[str]
    timestamp_utc_iso: str
    tickers: list[str]


def upsert_discord_row(
    session: Session,
    *,
    message_id: str,
    channel_id: str,
    author: Optional[str],
    content: Optional[str],
    when: dt.datetime,
    tickers: list[str],
) -> None:
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    else:
        when = when.astimezone(dt.timezone.utc)

    row = DiscordMessageRow(
        id=message_id,
        channel_id=channel_id,
        author=author,
        content=content,
        timestamp=when,
        tickers=tickers,
        processed=False,
    )
    merged = session.merge(row)
    session.commit()
    logger.debug("Upsert discord message id=%s channel=%s", merged.id, merged.channel_id)


def delete_messages_older_than(
    session: Session, *, cutoff: dt.datetime
) -> int:
    stmt = delete(DiscordMessageRow).where(DiscordMessageRow.timestamp < cutoff)
    result = session.execute(stmt)
    session.commit()
    return int(result.rowcount or 0)


def cleanup_retention(session: Session, settings: Optional[Settings] = None) -> int:
    cfg = settings or get_settings()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=cfg.retention_days)
    return delete_messages_older_than(session, cutoff=cutoff)


def list_messages_recent(
    session: Session,
    *,
    ticker: Optional[str],
    hours: int,
    limit: int,
) -> list[StoredDiscordMessage]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max(1, hours))
    stmt: Select[tuple[DiscordMessageRow]] = (
        select(DiscordMessageRow)
        .where(DiscordMessageRow.timestamp >= since)
        .order_by(DiscordMessageRow.timestamp.desc())
        .limit(max(1, min(limit, 200)))
    )
    rows = list(session.scalars(stmt).all())
    if ticker:
        ticker_u = ticker.strip().upper()
        rows = [r for r in rows if ticker_u in list(r.tickers or [])]

    return [
        StoredDiscordMessage(
            id=r.id,
            channel_id=r.channel_id,
            author=r.author,
            content=r.content,
            timestamp_utc_iso=r.timestamp.astimezone(dt.timezone.utc).isoformat(),
            tickers=list(r.tickers or []),
        )
        for r in rows
    ]


def row_tickers_dump(tickers: list[str]) -> str:
    return json.dumps(tickers, separators=(",", ":"), ensure_ascii=False)
