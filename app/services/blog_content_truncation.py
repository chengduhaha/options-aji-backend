"""Truncate blog post bodies for non-member previews."""
from __future__ import annotations

import html as html_module
import re

_MEMBER_CUTOFF_MARKER = "<!-- MEMBER_CUTOFF -->"
_TAG_RE = re.compile(r"<[^>]+>")


def truncate_blog_content(
    body: str,
    *,
    content_format: str,
    ratio: float = 0.5,
) -> str:
    if not body:
        return body

    if _MEMBER_CUTOFF_MARKER in body:
        return body.split(_MEMBER_CUTOFF_MARKER, 1)[0].rstrip()

    plain_len = _visible_text_length(body)
    if plain_len <= 1:
        return body

    target_chars = max(1, int(plain_len * ratio))
    if target_chars >= plain_len:
        return body

    if content_format == "html":
        return _truncate_html_at_text_length(body, target_chars)
    return _truncate_plain_at_length(body, target_chars)


def _visible_text_length(text: str) -> int:
    without_tags = _TAG_RE.sub("", text)
    return len(html_module.unescape(without_tags))


def _truncate_plain_at_length(text: str, target_chars: int) -> str:
    counted = 0
    for index, _char in enumerate(text):
        counted += 1
        if counted < target_chars:
            continue
        paragraph_break = text.rfind("\n\n", 0, index + 1)
        if paragraph_break >= int(len(text) * 0.3):
            return text[:paragraph_break].rstrip()
        return text[: index + 1].rstrip()
    return text


def _truncate_html_at_text_length(html: str, target_chars: int) -> str:
    counted = 0
    index = 0
    parts: list[str] = []
    while index < len(html):
        if html[index] == "<":
            end = html.find(">", index)
            if end == -1:
                parts.append(html[index:])
                break
            parts.append(html[index : end + 1])
            index = end + 1
            continue
        parts.append(html[index])
        counted += 1
        index += 1
        if counted >= target_chars:
            break
    return "".join(parts).rstrip()
