#!/usr/bin/env python3
"""Backfill SQLite with recent Discord REST history.

Usage:
  cd options-aji-backend && PYTHONPATH=. python scripts/discord_backfill_recent.py --days 3

Reads DISCORD_BOT_TOKEN and DISCORD_CHANNEL_IDS from `.env`.

For a single channel override (例如「实时美股消息区」的频道 ID):

  PYTHONPATH=. python scripts/discord_backfill_recent.py --days 3 --channel-id 123...

"""

from __future__ import annotations

import argparse

from app.config import get_settings
from app.db.bootstrap import init_db
from app.db.session import SessionLocal
from app.ingest.discord_history_rest import backfill_configured_channels


def main() -> None:
    parser = argparse.ArgumentParser(description="Discord channel history REST backfill.")
    parser.add_argument("--days", type=float, default=3.0, help="How many days backwards")
    parser.add_argument(
        "--channel-id",
        action="append",
        default=None,
        help="Override channel id(s). Repeat flag for multiples.",
    )
    parser.add_argument(
        "--no-bots",
        action="store_true",
        help="Skip bot-authored messages (not recommended for TweetShift feeds).",
    )
    parsed = parser.parse_args()

    init_db()

    cfg = get_settings()

    envelope = backfill_configured_channels(
        session_factory=SessionLocal,
        token=cfg.discord_bot_token,
        channel_csv=cfg.discord_channel_ids,
        days=parsed.days,
        include_bots=not parsed.no_bots,
        channel_override=parsed.channel_id,
    )
    print(envelope)


if __name__ == "__main__":
    main()
