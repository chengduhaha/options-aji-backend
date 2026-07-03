"""Apply English translations to blog posts missing EN content.

Loads translations from scripts/data/blog_en/{slug}.json.
Generate JSON with: python scripts/populate_blog_en.py --write-json

Run from repo root:
  python scripts/apply_blog_en_translations.py
  python scripts/apply_blog_en_translations.py --dry-run

On production, run against the server DB (same DATABASE_URL as the backend).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TypedDict

from sqlalchemy import select

from app.db.models_blog import BlogPostRow
from app.db import models_user  # noqa: F401 — register users table for FK
from app.db.session import SessionLocal

ROOT = Path(__file__).resolve().parent.parent
JSON_DIR = ROOT / "scripts" / "data" / "blog_en"

TARGET_SLUGS = [
    "atmgamma",
    "gamma",
    "gex",
    "ivrank",
    "liquidity",
    "oi",
    "premium",
    "seller",
    "sentiment",
    "serenity",
    "unusual",
    "volume",
]


class SlugTranslation(TypedDict):
    slug: str
    title_en: str
    excerpt_en: str
    body_en: str


def load_translation(slug: str) -> SlugTranslation | None:
    path = JSON_DIR / f"{slug}.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return SlugTranslation(
        slug=slug,
        title_en=str(data["title_en"]),
        excerpt_en=str(data.get("excerpt_en", "")),
        body_en=str(data["body_en"]),
    )


def apply_blog_en_translations(*, dry_run: bool = False) -> tuple[list[str], list[str]]:
    updated: list[str] = []
    failures: list[str] = []
    session = SessionLocal()
    try:
        for slug in TARGET_SLUGS:
            spec = load_translation(slug)
            if spec is None:
                failures.append(f"{slug}: missing {JSON_DIR / (slug + '.json')}")
                continue
            row = session.execute(select(BlogPostRow).where(BlogPostRow.slug == slug)).scalar_one_or_none()
            if row is None:
                failures.append(f"{slug}: post not found")
                continue
            if dry_run:
                print(
                    f"[dry-run] {slug}: title_en={spec['title_en'][:60]}... "
                    f"body_en={len(spec['body_en'])} chars"
                )
                updated.append(slug)
                continue
            row.title_en = spec["title_en"].strip()
            row.excerpt_en = spec["excerpt_en"].strip()
            row.body_en = spec["body_en"]
            session.add(row)
            updated.append(slug)
        if not dry_run:
            session.commit()
    except Exception as exc:
        session.rollback()
        failures.append(f"commit failed: {exc}")
    finally:
        session.close()
    return updated, failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply English blog translations")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB")
    args = parser.parse_args()
    updated, failures = apply_blog_en_translations(dry_run=args.dry_run)
    print(f"Updated: {len(updated)}")
    for slug in updated:
        print(f"  + {slug}")
    if failures:
        print(f"Failures: {len(failures)}")
        for msg in failures:
            print(f"  ! {msg}")


if __name__ == "__main__":
    main()
