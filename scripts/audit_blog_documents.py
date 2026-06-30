#!/usr/bin/env python3
"""Audit member document filenames and optionally backfill created_at from Baidu Netdisk."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.db.bootstrap import init_db
from app.db.models_blog import BlogAttachmentRow
from app.db.session import SessionLocal
from app.services.baidu_netdisk_import import DEFAULT_ROOTS, baidu_access_token_from_env, list_baidu_pdfs
from app.services.blog_document_sort import parse_filename_date, sort_documents


def _resolve_baidu_token(explicit: str | None) -> str | None:
    if explicit and explicit.strip():
        return explicit.strip()
    env_token = __import__("os").environ.get("BAIDU_NETDISK_ACCESS_TOKEN", "").strip()
    if env_token:
        return env_token
    codex_config = Path.home() / ".codex" / "config.toml"
    if not codex_config.is_file():
        return None
    for line in codex_config.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("BAIDU_NETDISK_ACCESS_TOKEN"):
            _, _, value = stripped.partition("=")
            token = value.strip().strip('"').strip("'")
            return token or None
    return None


def audit_documents(*, backfill: bool, token: str | None, limit: int | None) -> int:
    init_db()
    session = SessionLocal()
    try:
        rows = list(
            session.execute(select(BlogAttachmentRow).where(BlogAttachmentRow.post_id.is_(None))).scalars().all()
        )
        sorted_rows = sort_documents(rows)
        missing_date = [row for row in rows if parse_filename_date(row.original_filename) is None]
        missing_created_at = [row for row in rows if row.created_at is None]

        print(f"standalone_documents: {len(rows)}")
        print(f"missing_filename_date: {len(missing_date)}")
        print(f"missing_created_at: {len(missing_created_at)}")
        print("newest_10:")
        for row in sorted_rows[:10]:
            filename_date = parse_filename_date(row.original_filename)
            print(
                f"- id={row.id} category={row.category} "
                f"filename={row.original_filename!r} "
                f"filename_date={filename_date.isoformat() if filename_date else 'none'} "
                f"created_at={row.created_at.isoformat() if row.created_at else 'none'}"
            )

        if not backfill:
            if missing_created_at:
                print("Run with --backfill-baidu to set created_at from Baidu server_mtime.")
            return 0

        if not token:
            print("Baidu token required for backfill (BAIDU_NETDISK_ACCESS_TOKEN or ~/.codex/config.toml).", file=sys.stderr)
            return 1

        baidu_files = list_baidu_pdfs(token=token, roots=DEFAULT_ROOTS)
        by_key = {(file.filename, file.size): file for file in baidu_files}
        updated = 0
        targets = missing_created_at[:limit] if limit is not None else missing_created_at
        for row in targets:
            match = by_key.get((row.original_filename, row.file_size))
            if match is None or match.server_mtime <= 0:
                continue
            row.created_at = datetime.fromtimestamp(match.server_mtime, tz=timezone.utc)
            session.add(row)
            updated += 1
        if updated:
            session.commit()
        print(f"backfilled_created_at: {updated}")
        return 0
    finally:
        session.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit blog member document library ordering metadata.")
    parser.add_argument("--backfill-baidu", action="store_true", help="Backfill missing created_at from Baidu server_mtime.")
    parser.add_argument("--token", default=None, help="Baidu access token override.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum rows to backfill.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = _resolve_baidu_token(args.token)
    if args.backfill_baidu and not token:
        try:
            token = baidu_access_token_from_env()
        except RuntimeError:
            token = None
    return audit_documents(backfill=args.backfill_baidu, token=token, limit=args.limit)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
