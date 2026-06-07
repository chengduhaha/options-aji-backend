"""Discord/TweetShift author profiles — display names, bios, local avatars."""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import DiscordAuthorProfileRow
from app.services.discord_menu_authors import list_distinct_authors, resolve_author_filter

MAX_AVATAR_BYTES = 2 * 1024 * 1024
ALLOWED_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class AvatarValidationError(ValueError):
    pass


@dataclass(frozen=True)
class KolHubEntry:
    author: str
    display_name: str
    message_count: int
    last_seen_utc: str
    avatar_url: str | None
    bio_zh: str | None
    twitter_handle: str | None


def parse_display_name(author: str) -> str:
    raw = (author or "").strip()
    if not raw:
        return "未知来源"
    if "•" in raw:
        raw = raw.split("•", 1)[0].strip()
    return raw.lstrip("*").strip() or author.strip()


def avatar_storage_dir() -> Path:
    cfg = get_settings()
    base = Path(cfg.kol_avatars_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base


def avatar_public_url(filename: str | None) -> str | None:
    if not filename or not str(filename).strip():
        return None
    safe = Path(str(filename).strip()).name
    if not safe or safe.startswith("."):
        return None
    return f"/api/discord/avatars/{safe}"


def avatar_file_path(filename: str) -> Path:
    safe = Path(filename).name
    if not safe or safe.startswith(".."):
        raise AvatarValidationError("invalid_avatar_filename")
    return avatar_storage_dir() / safe


def get_or_create_profile(session: Session, author: str) -> DiscordAuthorProfileRow:
    row = session.get(DiscordAuthorProfileRow, author)
    if row is not None:
        return row
    row = DiscordAuthorProfileRow(
        author=author,
        display_name=parse_display_name(author),
    )
    session.add(row)
    session.flush()
    return row


def upsert_profile_fields(
    session: Session,
    *,
    author: str,
    display_name: str | None = None,
    bio_zh: str | None = None,
    twitter_handle: str | None = None,
    updated_by_user_id: str | None = None,
) -> DiscordAuthorProfileRow:
    author = author.strip()
    if not author:
        raise ValueError("author_required")
    row = get_or_create_profile(session, author)
    if display_name is not None:
        row.display_name = display_name.strip()[:128] or parse_display_name(author)
    if bio_zh is not None:
        row.bio_zh = bio_zh.strip()[:2000] or None
    if twitter_handle is not None:
        handle = twitter_handle.strip().lstrip("@").lower()
        row.twitter_handle = handle[:64] if handle else None
    if updated_by_user_id:
        row.updated_by_user_id = updated_by_user_id
    session.commit()
    session.refresh(row)
    return row


def save_avatar_file(
    session: Session,
    *,
    author: str,
    content: bytes,
    content_type: str,
    updated_by_user_id: str | None = None,
) -> DiscordAuthorProfileRow:
    author = author.strip()
    if not author:
        raise AvatarValidationError("author_required")
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct not in ALLOWED_CONTENT_TYPES:
        raise AvatarValidationError("unsupported_image_type")
    if len(content) > MAX_AVATAR_BYTES:
        raise AvatarValidationError("image_too_large")

    row = get_or_create_profile(session, author)
    if row.avatar_filename:
        old = avatar_file_path(row.avatar_filename)
        if old.is_file():
            old.unlink(missing_ok=True)

    digest = hashlib.sha256(author.encode("utf-8")).hexdigest()[:12]
    ext = EXT_BY_TYPE[ct]
    filename = f"{digest}-{uuid.uuid4().hex[:8]}{ext}"
    target = avatar_file_path(filename)
    target.write_bytes(content)

    row.avatar_filename = filename
    if updated_by_user_id:
        row.updated_by_user_id = updated_by_user_id
    session.commit()
    session.refresh(row)
    return row


def delete_avatar_file(session: Session, *, author: str) -> DiscordAuthorProfileRow:
    author = author.strip()
    if not author:
        raise ValueError("author_required")
    row = get_or_create_profile(session, author)
    if row.avatar_filename:
        path = avatar_file_path(row.avatar_filename)
        if path.is_file():
            path.unlink(missing_ok=True)
        row.avatar_filename = None
        session.commit()
        session.refresh(row)
    return row


def _profile_map(session: Session, authors: list[str]) -> dict[str, DiscordAuthorProfileRow]:
    if not authors:
        return {}
    rows = list(
        session.scalars(
            select(DiscordAuthorProfileRow).where(DiscordAuthorProfileRow.author.in_(authors))
        ).all()
    )
    return {r.author: r for r in rows}


def list_kol_hub(session: Session, *, menu_slot: str, hours: int) -> list[KolHubEntry]:
    menu_authors = resolve_author_filter(session, menu_slot)
    all_stats = list_distinct_authors(session)
    if menu_authors is not None:
        allowed = set(menu_authors)
        stats = [s for s in all_stats if s.author in allowed]
    else:
        stats = list(all_stats)

    profiles = _profile_map(session, [s.author for s in stats])
    out: list[KolHubEntry] = []
    for s in stats:
        prof = profiles.get(s.author)
        display = (prof.display_name if prof and prof.display_name else None) or parse_display_name(s.author)
        avatar_url = avatar_public_url(prof.avatar_filename if prof else None)
        out.append(
            KolHubEntry(
                author=s.author,
                display_name=display,
                message_count=s.message_count,
                last_seen_utc=s.last_seen_utc,
                avatar_url=avatar_url,
                bio_zh=(prof.bio_zh if prof else None),
                twitter_handle=(prof.twitter_handle if prof else None),
            )
        )
    return out


def list_admin_author_profiles(session: Session) -> list[dict[str, object]]:
    stats = list_distinct_authors(session)
    profiles = _profile_map(session, [s.author for s in stats])
    rows: list[dict[str, object]] = []
    for s in stats:
        prof = profiles.get(s.author)
        rows.append(
            {
                "author": s.author,
                "display_name": (prof.display_name if prof else None) or parse_display_name(s.author),
                "message_count": s.message_count,
                "last_seen_utc": s.last_seen_utc,
                "avatar_url": avatar_public_url(prof.avatar_filename if prof else None),
                "bio_zh": prof.bio_zh if prof else None,
                "twitter_handle": prof.twitter_handle if prof else None,
            }
        )
    return rows


def merge_author_filters(
    menu_authors: list[str] | None,
    requested: list[str] | None,
) -> list[str] | None:
    """Intersect menu whitelist with user-selected authors."""
    if not requested:
        return menu_authors
    cleaned = [a.strip() for a in requested if a.strip()]
    if not cleaned:
        return menu_authors
    if menu_authors is None:
        return list(dict.fromkeys(cleaned))
    allowed = set(menu_authors)
    merged = [a for a in cleaned if a in allowed]
    return merged if merged else menu_authors


def parse_authors_csv(raw: str | None) -> list[str] | None:
    if not raw or not raw.strip():
        return None
    parts = [p.strip() for p in re.split(r",", raw) if p.strip()]
    return list(dict.fromkeys(parts)) if parts else None
