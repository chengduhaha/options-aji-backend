"""Historical channel messages via Discord REST (guild channel message history).

Live gateway ingest only sees messages while the bot is online. This module
backfills older rows into SQLite so the UI can render multi-day archives.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Callable, Optional

import httpx
from sqlalchemy.orm import Session

from app.ingest.discord_bot import parse_channel_ids
from app.ingest.message_store import upsert_discord_row
from app.ingest.tickers import extract_tickers

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"

SessionFactory = Callable[[], Session]

def _auth_headers(bot_token: str) -> dict[str, str]:
    return {"Authorization": f"Bot {bot_token.strip()}"}


def _parse_ts(raw: object) -> dt.datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        raw_s = raw
        if raw_s.endswith("Z"):
            raw_s = raw_s.replace("Z", "+00:00", 1)
        d = dt.datetime.fromisoformat(raw_s)
        return d.astimezone(dt.timezone.utc) if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _message_plaintext(message: dict[str, object]) -> str | None:
    body = message.get("content")
    base = "" if body is None else str(body).strip()

    attachments = message.get("attachments")
    if isinstance(attachments, list) and attachments:
        parts: list[str] = []
        for item in attachments:
            if isinstance(item, dict):
                fname = item.get("filename")
                parts.append(fname if fname else "[file]")
            else:
                parts.append("[attachment]")
        attach_line = "[附件] " + ", ".join(parts)
        if base:
            return f"{base}\n{attach_line}"
        return attach_line

    if not base:
        return None
    return base[:8192]


def _author_label(author_obj: dict[str, object]) -> str | None:
    global_name = author_obj.get("global_name")
    username = author_obj.get("username")
    if isinstance(global_name, str) and global_name.strip():
        return global_name.strip()
    if isinstance(username, str) and username.strip():
        return username.strip()
    aid = author_obj.get("id")
    if aid is not None:
        return f"author-{aid}"
    return None


def fetch_channel_messages_page(
    *,
    token: str,
    channel_id: str,
    before: Optional[str] = None,
) -> tuple[list[dict[str, object]], Optional[float]]:
    """Return one batch (newest first) plus Retry-After seconds if HTTP 429."""

    params: dict[str, str | int] = {"limit": 100}
    if before:
        params["before"] = before

    with httpx.Client(timeout=45.0) as client:
        resp = client.get(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            headers=_auth_headers(token),
            params=params,
        )

    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After", "3")
        try:
            delay = float(retry)
        except ValueError:
            delay = 3.0
        logger.warning(
            "Discord rate limited fetching channel=%s Retry-After=%s",
            channel_id,
            retry,
        )
        return [], delay

    resp.raise_for_status()
    decoded = resp.json()
    rows: list[dict[str, object]] = decoded if isinstance(decoded, list) else []
    return rows, None


def backfill_recent_for_channel(
    session_factory: SessionFactory,
    *,
    token: str,
    channel_id: str,
    days: float,
    include_bots: bool = True,
) -> tuple[int, int]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max(1.0, float(days)))

    persisted = 0
    seen_ok = 0
    before: Optional[str] = None

    while True:
        page, backoff = fetch_channel_messages_page(
            token=token,
            channel_id=channel_id,
            before=before,
        )

        if backoff is not None:
            time.sleep(backoff + 0.5)
            continue

        if not page:
            break

        hit_cutoff = False

        for msg_obj in page:
            if not isinstance(msg_obj, dict):
                continue
            snowflake = msg_obj.get("id")
            if snowflake is None:
                continue
            message_id = str(snowflake)

            created_at_raw = msg_obj.get("timestamp")
            ts = _parse_ts(created_at_raw)
            if ts is None:
                continue

            if ts < cutoff:
                hit_cutoff = True
                break

            author_payload = msg_obj.get("author")
            if not isinstance(author_payload, dict):
                continue

            if not include_bots and bool(author_payload.get("bot")):
                continue

            plaintext = _message_plaintext(msg_obj)
            if plaintext is None:
                continue

            author = _author_label(author_payload)
            tickers = extract_tickers(plaintext)

            sess = session_factory()
            try:
                upsert_discord_row(
                    sess,
                    message_id=message_id,
                    channel_id=channel_id,
                    author=author,
                    content=plaintext,
                    when=ts,
                    tickers=tickers,
                )
            finally:
                sess.close()

            persisted += 1
            seen_ok += 1

        last_msg = page[-1]
        last_id_raw = last_msg.get("id") if isinstance(last_msg, dict) else None
        before = None if last_id_raw is None else str(last_id_raw)

        if hit_cutoff or len(page) < 100:
            break

    logger.info(
        "Backfill channel=%s rows=%s msgs_scanned_like=%s cutoff=%s",
        channel_id,
        persisted,
        seen_ok,
        cutoff.isoformat(),
    )
    return seen_ok, persisted


def backfill_configured_channels(
    *,
    session_factory: SessionFactory,
    token: str,
    channel_csv: str,
    days: float,
    include_bots: bool,
    channel_override: Optional[list[str]] = None,
) -> dict[str, object]:
    if not token.strip():
        raise ValueError("missing_bot_token")

    parsed = sorted(parse_channel_ids(channel_csv))
    targets = sorted({cid.strip() for cid in channel_override}) if channel_override else parsed
    if not targets:
        raise ValueError("no_channel_targets")

    per_channel: dict[str, dict[str, int]] = {}
    total_seen = 0
    total_rows = 0

    for ch in targets:
        seen, persisted = backfill_recent_for_channel(
            session_factory,
            token=token,
            channel_id=str(ch),
            days=days,
            include_bots=include_bots,
        )
        per_channel[ch] = {"seen_matches": seen, "persisted": persisted}
        total_seen += seen
        total_rows += persisted

    return {
        "channels": per_channel,
        "total_messages_persisted": total_rows,
        "approx_rows_touched_estimate": total_seen,
    }
