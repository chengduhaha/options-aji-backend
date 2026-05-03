"""Extract likely US ticker symbols from free text."""

from __future__ import annotations

import re

_NOISE_UPPER = frozenset(
    {
        "USD",
        "THE",
        "AND",
        "FOR",
        "ARE",
        "WAS",
        "WERE",
        "NOT",
        "ALL",
        "CAN",
        "HAS",
        "HAD",
        "ITS",
        "OUR",
        "OUT",
        "DAY",
        "API",
        "EPS",
        "CEO",
        "CFO",
        "IPO",
        "ETF",
        "IMO",
        "TLDR",
        "FYI",
    }
)


def extract_tickers(text: str, *, max_symbols: int = 12) -> list[str]:
    if not text or not text.strip():
        return []
    caps = r"[A-Z]{1}[A-Z0-9]{0,6}"
    found: set[str] = set()
    upper = text.upper()
    for m in re.finditer(rf"\$({caps})\b", upper):
        found.add(_normalize_symbol(m.group(1)))
    for m in re.finditer(rf"\b({caps})\b", upper):
        sym = _normalize_symbol(m.group(1))
        if sym in _NOISE_UPPER or len(sym) < 2:
            continue
        found.add(sym)
    ordered = sorted(found)
    return ordered[:max_symbols]


def _normalize_symbol(sym: str) -> str:
    s = sym.strip().upper().rstrip(".")
    while s.endswith("USD"):
        s = s[:-3].rstrip(".")
    return s
