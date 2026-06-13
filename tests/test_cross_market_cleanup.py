"""Cross-market cleanup guardrails."""
from __future__ import annotations

from pathlib import Path

from app.services.site_nav import KNOWN_NAV_IDS, NAV_GROUP_CHILDREN


ROOT = Path(__file__).resolve().parents[1]


def test_site_nav_uses_current_cross_market_menu_ids() -> None:
    assert "cross_xpoz" in KNOWN_NAV_IDS
    assert "cross_scanner" not in KNOWN_NAV_IDS
    assert "cross_feed" not in KNOWN_NAV_IDS
    assert NAV_GROUP_CHILDREN["cross_market_group"] == ("cross_market", "cross_xpoz")


def test_production_code_does_not_write_cursor_debug_logs() -> None:
    checked = [
        ROOT / "app" / "analytics" / "gex_compute.py",
        ROOT / "app" / "clients" / "futu_client.py",
    ]
    for path in checked:
        source = path.read_text(encoding="utf-8")
        assert ".cursor/debug" not in source
        assert "agent log" not in source
