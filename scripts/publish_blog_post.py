"""Publish an HTML blog post from scripts/data/blog_posts/{slug}.html (production DB)."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.db import models_user  # noqa: F401 — register users table for FK
from app.db.models_blog import BlogPostRow
from app.db.session import SessionLocal
from app.api.routes.blog import _invalidate_blog_public_cache

DATA_DIR = Path(__file__).resolve().parent / "data" / "blog_posts"
MANIFEST_DIR = DATA_DIR / "manifests"


def _load_manifest(slug: str) -> dict[str, object]:
    path = MANIFEST_DIR / f"{slug}.json"
    if not path.is_file():
        raise FileNotFoundError(f"Missing manifest: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def publish(*, slug: str, force: bool = False) -> str:
    meta = _load_manifest(slug)
    html_path = DATA_DIR / f"{slug}.html"
    if not html_path.is_file():
        raise FileNotFoundError(f"Missing HTML: {html_path}")

    body_zh = html_path.read_text(encoding="utf-8")
    now = datetime.now(timezone.utc)

    post_fields = {
        "slug": slug,
        "title_zh": str(meta["title_zh"]),
        "title_en": meta.get("title_en"),
        "excerpt_zh": meta.get("excerpt_zh"),
        "excerpt_en": meta.get("excerpt_en"),
        "category": str(meta.get("category", "general")),
        "tags": meta.get("tags", []),
        "content_format": str(meta.get("content_format", "html")),
        "status": str(meta.get("status", "published")),
    }

    session = SessionLocal()
    try:
        existing = session.execute(
            select(BlogPostRow).where(BlogPostRow.slug == slug)
        ).scalar_one_or_none()

        if existing and not force:
            print(f"Already exists: {slug} (id={existing.id})")
            return existing.id

        if existing and force:
            row = existing
            for key, value in post_fields.items():
                if key != "slug":
                    setattr(row, key, value)
            if isinstance(post_fields["tags"], list):
                row.tags = ",".join(str(t).strip() for t in post_fields["tags"] if str(t).strip())
            row.body_zh = body_zh
            row.published_at = row.published_at or now
            action = "updated"
        else:
            tags = post_fields.pop("tags", [])
            tag_str = ",".join(str(t).strip() for t in tags if str(t).strip()) if isinstance(tags, list) else ""
            row = BlogPostRow(
                **post_fields,
                tags=tag_str,
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
    parser = argparse.ArgumentParser(description="Publish HTML blog post from manifest + HTML file")
    parser.add_argument("slug", help="Post slug (must match manifest and HTML filename)")
    parser.add_argument("--force", action="store_true", help="Update if slug exists")
    args = parser.parse_args()
    publish(slug=args.slug, force=args.force)


if __name__ == "__main__":
    main()
