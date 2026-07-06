"""Tests for blog content truncation."""
from __future__ import annotations

from app.services.blog_content_truncation import truncate_blog_content


def test_truncate_markdown_at_half() -> None:
    body = "A" * 40 + "\n\n" + "B" * 40
    result = truncate_blog_content(body, content_format="markdown", ratio=0.5)
    assert len(result) < len(body)
    assert result.startswith("A")


def test_truncate_html_at_half() -> None:
    body = "<p>" + ("word " * 100) + "</p><p>hidden tail</p>"
    result = truncate_blog_content(body, content_format="html", ratio=0.5)
    assert "hidden tail" not in result
    assert result.startswith("<p>")


def test_member_cutoff_marker() -> None:
    body = "visible<!-- MEMBER_CUTOFF -->secret"
    result = truncate_blog_content(body, content_format="markdown", ratio=0.5)
    assert result == "visible"
    assert "secret" not in result
