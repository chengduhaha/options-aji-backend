"""Discord author whitelist per menu slot — admin configurable."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import DiscordMenuAuthorSettingsRow, DiscordMessageRow
from app.services.cache_service import TTL_HOT, cache_delete, cache_get, cache_set

CACHE_KEY_SETTINGS = "site:discord_menu_authors"

KNOWN_DISCORD_MENU_SLOTS: tuple[str, ...] = (
    "aji_insights",
    "feed",
    "twitter_kol",
    "ai",
    "messages",
)

DISCORD_MENU_SLOT_LABELS: dict[str, str] = {
    "aji_insights": "市场洞察",
    "feed": "统一信息流",
    "twitter_kol": "Twitter美股大牛追踪",
    "ai": "AI 分析师",
    "messages": "Discord 存档 / Messages API",
}


@dataclass(frozen=True)
class DiscordAuthorStat:
    author: str
    message_count: int
    last_seen_utc: str


def default_settings() -> dict[str, list[str]]:
    return {slot: [] for slot in KNOWN_DISCORD_MENU_SLOTS}


def normalize_settings(raw: Optional[dict[str, object]]) -> dict[str, list[str]]:
    base = default_settings()
    if not raw:
        return base
    for slot in KNOWN_DISCORD_MENU_SLOTS:
        val = raw.get(slot)
        if isinstance(val, list):
            authors = [str(a).strip() for a in val if str(a).strip()]
            base[slot] = list(dict.fromkeys(authors))
    return base


def merge_settings_update(
    current: dict[str, list[str]],
    patch: dict[str, list[str]],
) -> dict[str, list[str]]:
    out = dict(current)
    for slot, authors in patch.items():
        if slot not in KNOWN_DISCORD_MENU_SLOTS:
            continue
        cleaned = [str(a).strip() for a in authors if str(a).strip()]
        out[slot] = list(dict.fromkeys(cleaned))
    return out


def list_distinct_authors(
    session: Session,
    *,
    authors: Optional[list[str]] = None,
) -> list[DiscordAuthorStat]:
    stmt = (
        select(
            DiscordMessageRow.author,
            func.count().label("cnt"),
            func.max(DiscordMessageRow.timestamp).label("last_seen"),
        )
        .where(DiscordMessageRow.author.isnot(None))
        .where(DiscordMessageRow.author != "")
    )
    if authors:
        cleaned = [a.strip() for a in authors if a and str(a).strip()]
        if cleaned:
            stmt = stmt.where(DiscordMessageRow.author.in_(cleaned))
    stmt = stmt.group_by(DiscordMessageRow.author).order_by(func.max(DiscordMessageRow.timestamp).desc())
    rows = session.execute(stmt).all()
    out: list[DiscordAuthorStat] = []
    for author, cnt, last_seen in rows:
        label = str(author or "").strip()
        if not label:
            continue
        ts = last_seen
        if ts is None:
            iso = ""
        elif getattr(ts, "tzinfo", None) is None:
            iso = ts.replace(tzinfo=timezone.utc).isoformat()
        else:
            iso = ts.astimezone(timezone.utc).isoformat()
        out.append(
            DiscordAuthorStat(
                author=label,
                message_count=int(cnt or 0),
                last_seen_utc=iso,
            )
        )
    return out


def load_all_settings(session: Session) -> dict[str, list[str]]:
    cached = cache_get(CACHE_KEY_SETTINGS)
    if isinstance(cached, dict):
        return normalize_settings(cached)

    rows = list(session.scalars(select(DiscordMenuAuthorSettingsRow)).all())
    merged = default_settings()
    for row in rows:
        if row.menu_slot in KNOWN_DISCORD_MENU_SLOTS:
            merged[row.menu_slot] = normalize_settings({row.menu_slot: row.allowed_authors})[
                row.menu_slot
            ]
    cache_set(CACHE_KEY_SETTINGS, merged, ttl=TTL_HOT)
    return merged


def save_settings(
    session: Session,
    settings: dict[str, list[str]],
    *,
    updated_by_user_id: Optional[str] = None,
) -> dict[str, list[str]]:
    normalized = normalize_settings(settings)
    for slot in KNOWN_DISCORD_MENU_SLOTS:
        row = session.get(DiscordMenuAuthorSettingsRow, slot)
        authors = normalized[slot]
        if row is None:
            session.add(
                DiscordMenuAuthorSettingsRow(
                    menu_slot=slot,
                    allowed_authors=authors,
                    updated_by_user_id=updated_by_user_id,
                )
            )
        else:
            row.allowed_authors = authors
            if updated_by_user_id:
                row.updated_by_user_id = updated_by_user_id
    session.commit()
    cache_delete(CACHE_KEY_SETTINGS)
    cache_set(CACHE_KEY_SETTINGS, normalized, ttl=TTL_HOT)
    return normalized


def resolve_author_filter(session: Session, menu_slot: Optional[str]) -> Optional[list[str]]:
    """Return author whitelist for SQL IN (...), or None when no filter applies."""
    if not menu_slot or menu_slot not in KNOWN_DISCORD_MENU_SLOTS:
        return None
    settings = load_all_settings(session)
    authors = settings.get(menu_slot) or []
    if not authors:
        return None
    return authors


def seed_discord_menu_author_settings(session: Session) -> None:
    for slot in KNOWN_DISCORD_MENU_SLOTS:
        if session.get(DiscordMenuAuthorSettingsRow, slot) is None:
            session.add(
                DiscordMenuAuthorSettingsRow(
                    menu_slot=slot,
                    allowed_authors=[],
                )
            )
    session.commit()
