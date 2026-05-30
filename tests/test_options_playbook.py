"""Tests for options playbook skill loader."""
from __future__ import annotations

from app.services.options_playbook import (
    PLAYBOOK_DIR,
    build_playbook_context_blob,
    get_playbook_hints,
    list_sections,
    select_sections_for_context,
)


def test_playbook_artifacts_exist() -> None:
    assert (PLAYBOOK_DIR / "playbook.html").is_file()
    assert (PLAYBOOK_DIR / "sections.json").is_file()
    assert (PLAYBOOK_DIR / "SKILL.md").is_file()


def test_list_sections_has_six() -> None:
    sections = list_sections()
    assert len(sections) >= 6
    ids = {s.id for s in sections}
    assert "section1" in ids
    assert "section5" in ids


def test_select_sections_mvp_with_unusual() -> None:
    ids = select_sections_for_context(unusual_count=2, mode="mvp")
    assert "section5" in ids
    assert "section4" in ids


def test_select_sections_strategy_mode() -> None:
    ids = select_sections_for_context(mode="strategy")
    assert "section1" in ids
    assert "section3" in ids


def test_build_blob_truncates() -> None:
    blob = build_playbook_context_blob(["section1", "section4_2"], max_chars=500)
    assert len(blob) <= 520
    assert "section" in blob or "策略" in blob or "IV" in blob


def test_playbook_hints() -> None:
    h = get_playbook_hints("expected_move")
    assert h.topic == "expected_move"
    assert len(h.bullets) >= 2
