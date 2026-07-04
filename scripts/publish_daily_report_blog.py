"""Publish a daily market report as an HTML blog post (production DB)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.db import models_user  # noqa: F401 — register users table for FK
from app.db.models_blog import BlogPostRow
from app.db.session import SessionLocal
from app.api.routes.blog import _invalidate_blog_public_cache

DATA_DIR = Path(__file__).resolve().parent / "data" / "blog_posts"

POST = {
    "slug": "0dte-daily-2026-07-02",
    "title_zh": "0DTE与热门正股暗流情报日报 · 2026年7月2日",
    "title_en": "0DTE & Dark-Flow Daily Brief · July 2, 2026",
    "excerpt_zh": (
        "非农爆冷、芯片暴力去杠杆、特斯拉「利好出尽」——三个事件重塑今日 0DTE 结构。"
        "SPY 正 Gamma 托底 $741，QQQ $720 Put 爆量 1.7 万张；10 只热门股一张表读懂暗流。"
    ),
    "excerpt_en": (
        "NFP miss, chip deleveraging, and TSLA sell-the-news reshaped 0DTE structure. "
        "SPY positive gamma at $741; QQQ $720 Put swept 17k contracts — condensed dark-flow brief."
    ),
    "category": "daily-report",
    "tags": ["0DTE", "暗流", "日报", "期权", "市场报告"],
    "content_format": "html",
    "status": "published",
}


def publish(*, force: bool = False) -> str:
    html_path = DATA_DIR / f"{POST['slug']}.html"
    if not html_path.is_file():
        raise FileNotFoundError(f"Missing HTML: {html_path}")

    body_zh = html_path.read_text(encoding="utf-8")
    now = datetime.now(timezone.utc)

    session = SessionLocal()
    try:
        existing = session.execute(
            select(BlogPostRow).where(BlogPostRow.slug == POST["slug"])
        ).scalar_one_or_none()

        if existing and not force:
            print(f"Already exists: {POST['slug']} (id={existing.id})")
            return existing.id

        if existing and force:
            row = existing
            for key, value in POST.items():
                if key != "slug":
                    setattr(row, key, value)
            row.body_zh = body_zh
            row.published_at = row.published_at or now
            action = "updated"
        else:
            row = BlogPostRow(
                **POST,
                body_zh=body_zh,
                published_at=now,
            )
            session.add(row)
            action = "created"

        session.commit()
        session.refresh(row)
        _invalidate_blog_public_cache()
        print(f"{action} post id={row.id} slug={row.slug} status={row.status}")
        return row.id
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish daily report HTML blog post")
    parser.add_argument("--force", action="store_true", help="Update if slug exists")
    args = parser.parse_args()
    publish(force=args.force)


if __name__ == "__main__":
    main()
