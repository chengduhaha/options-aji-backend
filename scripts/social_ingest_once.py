"""Run one-off social ingestion smoke check."""

from __future__ import annotations

import argparse

from app.db.models import SocialPostRow, TickerSentimentSnapshotRow
from app.db.session import SessionLocal
from app.services.social_sentiment import (
    get_social_radar,
    ingest_kol_timeline_posts,
    ingest_social_snapshot_for_symbol,
    ingest_social_snapshots,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run social sentiment ingestion once.")
    parser.add_argument("--limit", type=int, default=5, help="Radar item limit")
    parser.add_argument("--symbol", type=str, default="", help="Optional single symbol run")
    parser.add_argument("--kol", action="store_true", help="Also ingest KOL timelines (xpoz)")
    args = parser.parse_args()

    if args.symbol.strip():
        ingest_social_snapshot_for_symbol(args.symbol)
    else:
        ingest_social_snapshots()
    if args.kol:
        ingest_kol_timeline_posts()
    radar = get_social_radar(limit=max(1, args.limit))
    print("radar_items", len(radar.items))
    print("top", [(item.symbol, item.mentions_24h, item.sentiment_score) for item in radar.items[:3]])

    with SessionLocal() as session:
        social_posts = session.query(SocialPostRow).count()
        sentiment_rows = session.query(TickerSentimentSnapshotRow).count()
    print("social_posts", social_posts)
    print("sentiment_rows", sentiment_rows)


if __name__ == "__main__":
    main()
