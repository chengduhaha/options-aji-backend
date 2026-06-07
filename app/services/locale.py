"""Locale helpers for bilingual API responses."""

from __future__ import annotations

from typing import Literal, Sequence

Locale = Literal["zh", "en"]
DEFAULT_LOCALE: Locale = "zh"


def parse_locale(value: str | None) -> Locale:
    return "en" if value == "en" else DEFAULT_LOCALE


def pick_text(
    *,
    zh: str | None,
    en: str | None,
    raw: str | None = None,
    locale: Locale,
) -> str:
    if locale == "en":
        for candidate in (en, raw, zh):
            if candidate and candidate.strip():
                return candidate.strip()
        return ""
    for candidate in (zh, raw, en):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def pick_list(
    *,
    zh: Sequence[str] | None,
    en: Sequence[str] | None,
    locale: Locale,
) -> list[str]:
    if locale == "en":
        if en:
            return [str(item).strip() for item in en if str(item).strip()]
        if zh:
            return [str(item).strip() for item in zh if str(item).strip()]
        return []
    if zh:
        return [str(item).strip() for item in zh if str(item).strip()]
    if en:
        return [str(item).strip() for item in en if str(item).strip()]
    return []
