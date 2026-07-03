"""Report bilingual coverage for blog posts and optionally refresh seed content."""
from __future__ import annotations

import argparse

from sqlalchemy import select

from app.db.models_blog import BlogPostRow
from app.db.session import SessionLocal


def _has_en(row: BlogPostRow) -> bool:
    return bool((row.body_en or "").strip() or (row.title_en or "").strip())


def report_bilingual(*, apply_seed: bool = False) -> None:
    session = SessionLocal()
    try:
        rows = session.execute(select(BlogPostRow).order_by(BlogPostRow.slug)).scalars().all()
        if not rows:
            print("No blog posts found.")
            return

        missing_en = [row for row in rows if not _has_en(row)]
        print(f"Total posts: {len(rows)}")
        print(f"With English content: {len(rows) - len(missing_en)}")
        print(f"Missing English content: {len(missing_en)}")
        for row in missing_en:
            print(f"  - {row.slug} ({row.status})")

        if apply_seed:
            from scripts.seed_blog import seed_blog_posts

            updated = seed_blog_posts(force=True)
            print(f"Seed refresh updated {updated} post(s).")
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Report blog bilingual coverage")
    parser.add_argument(
        "--apply-seed",
        action="store_true",
        help="Re-apply seed_blog.py content (updates known seed slugs with EN copy)",
    )
    args = parser.parse_args()
    report_bilingual(apply_seed=args.apply_seed)


if __name__ == "__main__":
    main()
