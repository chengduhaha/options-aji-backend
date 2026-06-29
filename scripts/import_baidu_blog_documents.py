#!/usr/bin/env python3
"""Import Baidu Netdisk PDFs into the member document library."""
from __future__ import annotations

import argparse
import sys

from app.db.bootstrap import init_db
from app.db.session import SessionLocal
from app.services.baidu_netdisk_import import (
    DEFAULT_ROOTS,
    baidu_access_token_from_env,
    import_baidu_documents,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import Baidu Netdisk PDFs into blog member documents.")
    parser.add_argument(
        "--root",
        action="append",
        dest="roots",
        help="Baidu Netdisk root directory to scan. May be passed multiple times.",
    )
    parser.add_argument("--dry-run", action="store_true", default=True, help="Plan import without downloading PDFs.")
    parser.add_argument(
        "--no-dry-run",
        action="store_false",
        dest="dry_run",
        help="Download and insert PDFs. Requires --yes.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Maximum new PDFs to import.")
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=2.0,
        help="Abort real import unless this many GB remain free after planned downloads.",
    )
    parser.add_argument("--yes", action="store_true", help="Required with --no-dry-run to perform writes.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.dry_run and not args.yes:
        parser.error("--no-dry-run requires --yes")
    roots = args.roots or list(DEFAULT_ROOTS)
    min_free_bytes = int(args.min_free_gb * 1024 * 1024 * 1024)

    token = baidu_access_token_from_env()
    init_db()
    session = SessionLocal()
    try:
        result = import_baidu_documents(
            session=session,
            token=token,
            roots=roots,
            dry_run=args.dry_run,
            limit=args.limit,
            min_free_bytes=min_free_bytes,
        )
    finally:
        session.close()

    mb = result.plan.import_bytes / 1024 / 1024
    print(f"roots: {', '.join(roots)}")
    print(f"dry_run: {result.dry_run}")
    print(f"planned_import_count: {result.plan.import_count}")
    print(f"planned_import_mb: {mb:.2f}")
    print(f"skipped_duplicates: {result.plan.skipped_duplicate_count}")
    print(f"skipped_existing: {result.plan.skipped_existing_count}")
    print(f"imported_count: {result.imported_count}")
    print(f"imported_mb: {result.imported_bytes / 1024 / 1024:.2f}")
    for item in result.plan.items[:20]:
        print(f"- [{item.category}] {item.file.filename} ({item.file.size} bytes)")
    if result.plan.import_count > 20:
        print(f"... {result.plan.import_count - 20} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
