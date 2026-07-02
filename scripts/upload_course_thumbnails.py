#!/usr/bin/env python3
"""Upload course cover images to R2 and update blog_attachments.thumbnail_stored_name."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

from app.db.bootstrap import init_db
from app.db.models_blog import BlogAttachmentRow
from app.db.session import SessionLocal
from app.services.blog_thumbnail import delete_thumbnail, store_thumbnail

ASSETS = Path("/root/.cursor/projects/root-workspace/assets")

TITLE_TO_IMAGE: dict[str, str] = {
    "会员期权课-spread双腿策略(二)": "ChatGPT_Image_2026_7_2__19_09_52__5_-fddc4879-bacf-4263-be7a-d05c33a52a92.png",
    "会员期权课-spread双腿策略(一)": "ChatGPT_Image_2026_7_2__19_09_52__7_-223d8825-3d23-468a-91ee-e63ba4f44315.png",
    "会员期权课-spread双腿策略(三)": "ChatGPT_Image_2026_7_2__19_09_52__6_-fab79471-7be8-4ddd-a15f-3e03b5c22249.png",
    "会员期权课-IronCondor&butterfly": "ChatGPT_Image_2026_7_2__19_09_52__4_-f6514d8b-5200-4961-b08e-646a0ec41763.png",
    "会员期权课-vix(下)": "ChatGPT_Image_2026_7_2__19_09_52__8_-78055381-99ff-4aca-85e4-cc8ba7a83510.png",
    "会员期权课-vix(上)": "ChatGPT_Image_2026_7_2__19_09_52__10_-4f294339-9b60-4c88-bd66-3c8d36c9b43f.png",
    "会员期权课-vix(中)": "ChatGPT_Image_2026_7_2__19_09_52__9_-80f50b25-db87-4b74-b102-d94ecf5eed49.png",
}


def upload_thumbnails(*, dry_run: bool) -> int:
    init_db()
    session = SessionLocal()
    updated = 0
    try:
        rows = list(
            session.execute(
                select(BlogAttachmentRow).where(
                    BlogAttachmentRow.post_id.is_(None),
                    BlogAttachmentRow.media_kind == "video",
                )
            ).scalars()
        )
        by_title = {row.title_zh: row for row in rows if row.title_zh}

        for title, filename in TITLE_TO_IMAGE.items():
            row = by_title.get(title)
            if row is None:
                print(f"SKIP missing course: {title}", file=sys.stderr)
                continue

            image_path = ASSETS / filename
            if not image_path.is_file():
                print(f"SKIP missing file: {image_path}", file=sys.stderr)
                continue

            content = image_path.read_bytes()
            print(f"{'DRY' if dry_run else 'UPLOAD'} {title} -> {row.id} ({len(content)} bytes)")

            if dry_run:
                continue

            stored_name = store_thumbnail(
                content=content,
                attachment_id=row.id,
                mime_type="image/png",
            )
            if row.thumbnail_stored_name and row.thumbnail_stored_name != stored_name:
                delete_thumbnail(row.thumbnail_stored_name)

            row.thumbnail_stored_name = stored_name
            session.add(row)
            updated += 1

        if not dry_run:
            session.commit()
        print(f"Done: {updated} thumbnails uploaded.")
        return 0 if updated or dry_run else 1
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload course cover images.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without uploading.")
    args = parser.parse_args()
    return upload_thumbnails(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
