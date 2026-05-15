"""KOL handle list parsing — no DB/session imports."""

from __future__ import annotations


def parse_kol_handles_csv(raw: str) -> list[str]:
    out: list[str] = []
    for part in raw.split(","):
        h = part.strip().lstrip("@").lower()
        if h:
            out.append(h)
    deduped: list[str] = []
    seen: set[str] = set()
    for h in out:
        if h in seen:
            continue
        seen.add(h)
        deduped.append(h)
    return deduped
