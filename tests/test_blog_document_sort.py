"""Tests for blog document filename date sorting."""
from __future__ import annotations

import datetime as dt

from app.db.models_blog import BlogAttachmentRow
from app.services.blog_document_sort import document_sort_key, parse_filename_date, sort_documents


def test_parse_filename_date_compact() -> None:
    parsed = parse_filename_date("阿吉生财有道_美股报告_20260520_v1.0.pdf")
    assert parsed == dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc)


def test_parse_filename_date_dashed() -> None:
    parsed = parse_filename_date("report_2026-05-20_member.pdf")
    assert parsed == dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc)


def test_parse_filename_date_dotted() -> None:
    parsed = parse_filename_date("report_2026.05.20.pdf")
    assert parsed == dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc)


def test_parse_filename_date_missing() -> None:
    assert parse_filename_date("course-notes.pdf") is None


def test_sort_documents_prefers_filename_date_over_created_at() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    older_name = BlogAttachmentRow(
        id="a",
        stored_name="a.pdf",
        original_filename="report_20260501.pdf",
        mime_type="application/pdf",
        file_size=1,
        created_at=now,
    )
    newer_name = BlogAttachmentRow(
        id="b",
        stored_name="b.pdf",
        original_filename="report_20260520.pdf",
        mime_type="application/pdf",
        file_size=1,
        created_at=now - dt.timedelta(days=30),
    )
    sorted_rows = sort_documents([older_name, newer_name])
    assert [row.id for row in sorted_rows] == ["b", "a"]


def test_sort_documents_falls_back_to_created_at() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    newer = BlogAttachmentRow(
        id="new",
        stored_name="new.pdf",
        original_filename="notes.pdf",
        mime_type="application/pdf",
        file_size=1,
        created_at=now,
    )
    older = BlogAttachmentRow(
        id="old",
        stored_name="old.pdf",
        original_filename="archive.pdf",
        mime_type="application/pdf",
        file_size=1,
        created_at=now - dt.timedelta(days=5),
    )
    sorted_rows = sort_documents([older, newer])
    assert [row.id for row in sorted_rows] == ["new", "old"]


def test_document_sort_key_tiebreaks_by_id() -> None:
    same_day = dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc)
    left = BlogAttachmentRow(
        id="bbb",
        stored_name="b.pdf",
        original_filename="report_20260520.pdf",
        mime_type="application/pdf",
        file_size=1,
        created_at=same_day,
    )
    right = BlogAttachmentRow(
        id="aaa",
        stored_name="a.pdf",
        original_filename="report_20260520.pdf",
        mime_type="application/pdf",
        file_size=1,
        created_at=same_day,
    )
    assert document_sort_key(left) > document_sort_key(right)


def test_sort_documents_prefers_dated_filename_over_recent_created_at() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    dated = BlogAttachmentRow(
        id="dated",
        stored_name="dated.pdf",
        original_filename="report_20260501.pdf",
        mime_type="application/pdf",
        file_size=1,
        created_at=now - dt.timedelta(days=365),
    )
    undated = BlogAttachmentRow(
        id="undated",
        stored_name="undated.pdf",
        original_filename="notes.pdf",
        mime_type="application/pdf",
        file_size=1,
        created_at=now,
    )
    sorted_rows = sort_documents([undated, dated])
    assert [row.id for row in sorted_rows] == ["dated", "undated"]
