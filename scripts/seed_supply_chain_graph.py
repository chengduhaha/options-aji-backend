#!/usr/bin/env python3
"""Seed curated supply-chain graph data.

Usage:
    python scripts/seed_supply_chain_graph.py
    python scripts/seed_supply_chain_graph.py scripts/spacex_supply_chain_2026_seed.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.bootstrap import init_db  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.graph.service import ingest_graph_payload  # noqa: E402


DEFAULT_SEED_PATH = Path(__file__).with_name("spacex_supply_chain_2026_seed.json")


def main() -> int:
    seed_path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_SEED_PATH
    payload = json.loads(seed_path.read_text(encoding="utf-8"))

    init_db()
    with SessionLocal() as session:
        result = ingest_graph_payload(session, payload)

    print(json.dumps({"success": True, "seed": str(seed_path), "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
