"""Discord menu author whitelist — admin configure, users read."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps_auth import get_current_admin_user, get_current_user
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.ingest.message_store import StoredDiscordFeedEntry, list_discord_feed_rows
from app.services.discord_author_profiles import (
    AvatarValidationError,
    avatar_file_path,
    delete_avatar_file,
    list_admin_author_profiles,
    list_kol_hub,
    merge_author_filters,
    parse_authors_csv,
    save_avatar_file,
    upsert_profile_fields,
)
from app.services.cache_service import TTL_HOT, cache_get, cache_set
from app.services.discord_menu_authors import (
    DISCORD_MENU_SLOT_LABELS,
    KNOWN_DISCORD_MENU_SLOTS,
    list_distinct_authors,
    load_all_settings,
    merge_settings_update,
    resolve_author_filter,
    save_settings,
)
from app.services.locale import Locale, parse_locale, pick_list, pick_text

router = APIRouter(tags=["discord-menu"])


class DiscordAuthorStatPayload(BaseModel):
    author: str
    message_count: int
    last_seen_utc: str


class DiscordAuthorsResponse(BaseModel):
    authors: list[DiscordAuthorStatPayload]
    generated_at_utc: str


class DiscordMenuAuthorsResponse(BaseModel):
    settings: dict[str, list[str]]
    known_slots: list[str] = Field(default_factory=lambda: list(KNOWN_DISCORD_MENU_SLOTS))
    slot_labels: dict[str, str] = Field(default_factory=lambda: dict(DISCORD_MENU_SLOT_LABELS))


class DiscordMenuAuthorsUpdateBody(BaseModel):
    settings: dict[str, list[str]] = Field(default_factory=dict)


class DiscordTimelineItem(BaseModel):
    id: str
    kind: str = "discord"
    created_at_utc: str
    title: str
    body: str
    tickers: list[str] = Field(default_factory=list)
    author: str | None = None
    raw_body: str | None = None
    bullets: list[str] | None = None
    bullets_zh: list[str] | None = None
    bullets_en: list[str] | None = None
    risk_note: str | None = None
    risk_note_zh: str | None = None
    risk_note_en: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None


class DiscordTimelineEnvelope(BaseModel):
    generated_at_utc: str
    menu_slot: str
    items: list[DiscordTimelineItem]
    next_before: str | None = None
    has_more: bool = False


class KolHubItemPayload(BaseModel):
    author: str
    display_name: str
    message_count: int
    last_seen_utc: str
    avatar_url: str | None = None
    bio: str | None = None
    bio_zh: str | None = None
    bio_en: str | None = None
    twitter_handle: str | None = None


class KolHubResponse(BaseModel):
    generated_at_utc: str
    menu_slot: str
    items: list[KolHubItemPayload]


class AuthorProfilePayload(BaseModel):
    author: str
    display_name: str
    message_count: int
    last_seen_utc: str
    avatar_url: str | None = None
    bio_zh: str | None = None
    twitter_handle: str | None = None


class AuthorProfilesResponse(BaseModel):
    items: list[AuthorProfilePayload]


class AuthorProfileUpdateBody(BaseModel):
    author: str
    display_name: str | None = None
    bio_zh: str | None = None
    twitter_handle: str | None = None


def _cursor_timestamp_iso(value: datetime) -> str:
    """URL-safe ISO cursor (Z suffix avoids '+' being decoded as space in query strings)."""
    utc = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return utc.isoformat().replace("+00:00", "Z")


def _parse_before_timestamp(raw: str | None) -> datetime | None:
    if not raw or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00")
    # Query strings decode '+' as space; recover offset if present.
    if " " in text and "+" not in text:
        text = text.replace(" ", "+", 1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _authors_cache_token(authors: list[str] | None) -> str:
    if not authors:
        return "all"
    return hashlib.sha256(",".join(sorted(authors)).encode()).hexdigest()[:16]


def _timeline_item_from_row(
    r: StoredDiscordFeedEntry,
    *,
    display_name: str | None = None,
    avatar_url: str | None = None,
    locale: Locale = "zh",
) -> DiscordTimelineItem:
    bullets_zh = [b for b in r.enrichment_bullets_zh if str(b).strip()]
    bullets_en = [b for b in r.enrichment_bullets_en if str(b).strip()]
    title = pick_text(
        zh=r.enrichment_title_zh,
        en=r.enrichment_title_en,
        raw=display_name or r.author or "Discord",
        locale=locale,
    )
    body = pick_text(
        zh=r.enrichment_summary_zh,
        en=r.enrichment_summary_en,
        raw=(r.content or "")[:2000],
        locale=locale,
    )[:4000]
    bullets = pick_list(zh=bullets_zh, en=bullets_en, locale=locale) or None
    risk_zh = (r.enrichment_risk_zh or "").strip() or None
    risk_en = (r.enrichment_risk_en or "").strip() or None
    risk_note = pick_text(zh=risk_zh, en=risk_en, locale=locale) or None
    return DiscordTimelineItem(
        id=f"dc-{r.id}",
        created_at_utc=r.timestamp_utc_iso,
        title=title or (display_name or r.author or "Discord"),
        body=body,
        tickers=list(r.tickers),
        author=r.author,
        raw_body=r.content,
        bullets=bullets,
        bullets_zh=bullets_zh or None,
        bullets_en=bullets_en or None,
        risk_note=risk_note,
        risk_note_zh=risk_zh,
        risk_note_en=risk_en,
        display_name=display_name,
        avatar_url=avatar_url,
    )


@router.get("/api/admin/discord/authors", response_model=DiscordAuthorsResponse)
def admin_list_discord_authors(
    session: Session = Depends(db_session_dep),
    _: UserRow = Depends(get_current_admin_user),
) -> DiscordAuthorsResponse:
    stats = list_distinct_authors(session)
    return DiscordAuthorsResponse(
        authors=[
            DiscordAuthorStatPayload(
                author=s.author,
                message_count=s.message_count,
                last_seen_utc=s.last_seen_utc,
            )
            for s in stats
        ],
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/api/admin/discord/author-profiles", response_model=AuthorProfilesResponse)
def admin_list_author_profiles(
    session: Session = Depends(db_session_dep),
    _: UserRow = Depends(get_current_admin_user),
) -> AuthorProfilesResponse:
    rows = list_admin_author_profiles(session)
    return AuthorProfilesResponse(
        items=[AuthorProfilePayload(**row) for row in rows],
    )


@router.put("/api/admin/discord/author-profiles", response_model=AuthorProfilePayload)
def admin_upsert_author_profile(
    body: AuthorProfileUpdateBody,
    admin: UserRow = Depends(get_current_admin_user),
    session: Session = Depends(db_session_dep),
) -> AuthorProfilePayload:
    row = upsert_profile_fields(
        session,
        author=body.author,
        display_name=body.display_name,
        bio_zh=body.bio_zh,
        twitter_handle=body.twitter_handle,
        updated_by_user_id=admin.id,
    )
    stats = {s.author: s for s in list_distinct_authors(session)}
    stat = stats.get(row.author)
    from app.services.discord_author_profiles import avatar_public_url

    return AuthorProfilePayload(
        author=row.author,
        display_name=row.display_name or body.author,
        message_count=stat.message_count if stat else 0,
        last_seen_utc=stat.last_seen_utc if stat else "",
        avatar_url=avatar_public_url(row.avatar_filename),
        bio_zh=row.bio_zh,
        twitter_handle=row.twitter_handle,
    )


@router.post("/api/admin/discord/author-profiles/avatar", response_model=AuthorProfilePayload)
async def admin_upload_author_avatar(
    author: str = Form(...),
    file: UploadFile = File(...),
    admin: UserRow = Depends(get_current_admin_user),
    session: Session = Depends(db_session_dep),
) -> AuthorProfilePayload:
    content = await file.read()
    try:
        row = save_avatar_file(
            session,
            author=author,
            content=content,
            content_type=file.content_type or "application/octet-stream",
            updated_by_user_id=admin.id,
        )
    except AvatarValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    stats = {s.author: s for s in list_distinct_authors(session)}
    stat = stats.get(row.author)
    from app.services.discord_author_profiles import avatar_public_url

    return AuthorProfilePayload(
        author=row.author,
        display_name=row.display_name or author,
        message_count=stat.message_count if stat else 0,
        last_seen_utc=stat.last_seen_utc if stat else "",
        avatar_url=avatar_public_url(row.avatar_filename),
        bio_zh=row.bio_zh,
        twitter_handle=row.twitter_handle,
    )


@router.delete("/api/admin/discord/author-profiles/avatar", response_model=AuthorProfilePayload)
def admin_delete_author_avatar(
    author: str = Query(...),
    admin: UserRow = Depends(get_current_admin_user),
    session: Session = Depends(db_session_dep),
) -> AuthorProfilePayload:
    row = delete_avatar_file(session, author=author)
    stats = {s.author: s for s in list_distinct_authors(session)}
    stat = stats.get(row.author)
    from app.services.discord_author_profiles import avatar_public_url

    return AuthorProfilePayload(
        author=row.author,
        display_name=row.display_name or author,
        message_count=stat.message_count if stat else 0,
        last_seen_utc=stat.last_seen_utc if stat else "",
        avatar_url=avatar_public_url(row.avatar_filename),
        bio_zh=row.bio_zh,
        twitter_handle=row.twitter_handle,
    )


@router.get("/api/discord/avatars/{filename}")
def serve_kol_avatar(filename: str) -> FileResponse:
    safe = Path(filename).name
    if not safe or safe.startswith("."):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    path = avatar_file_path(safe)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    media = "image/jpeg"
    if safe.endswith(".png"):
        media = "image/png"
    elif safe.endswith(".webp"):
        media = "image/webp"
    return FileResponse(path, media_type=media)


@router.get("/api/discord/kol-hub", response_model=KolHubResponse)
def discord_kol_hub(
    menu_slot: str = Query(default="twitter_kol"),
    hours: int = Query(default=168, ge=1, le=24 * 30),
    locale: str = Query(default="zh", pattern="^(zh|en)$"),
    session: Session = Depends(db_session_dep),
) -> KolHubResponse:
    loc = parse_locale(locale)
    cache_key = f"discord:kol-hub:v1:{menu_slot}:{hours}:{loc}"
    cached = cache_get(cache_key)
    if isinstance(cached, dict):
        return KolHubResponse.model_validate(cached)

    entries = list_kol_hub(session, menu_slot=menu_slot, hours=hours)
    response = KolHubResponse(
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
        menu_slot=menu_slot,
        items=[
            KolHubItemPayload(
                author=e.author,
                display_name=e.display_name,
                message_count=e.message_count,
                last_seen_utc=e.last_seen_utc,
                avatar_url=e.avatar_url,
                bio=pick_text(zh=e.bio_zh, en=e.bio_en, locale=loc) or None,
                bio_zh=e.bio_zh,
                bio_en=e.bio_en,
                twitter_handle=e.twitter_handle,
            )
            for e in entries
        ],
    )
    cache_set(cache_key, response.model_dump(), ttl=TTL_HOT)
    return response


@router.get("/api/site/discord-menu-authors", response_model=DiscordMenuAuthorsResponse)
def get_discord_menu_authors(
    session: Session = Depends(db_session_dep),
    _: UserRow = Depends(get_current_user),
) -> DiscordMenuAuthorsResponse:
    settings = load_all_settings(session)
    return DiscordMenuAuthorsResponse(settings=settings)


@router.put("/api/admin/discord-menu-authors", response_model=DiscordMenuAuthorsResponse)
def put_discord_menu_authors(
    body: DiscordMenuAuthorsUpdateBody,
    admin: UserRow = Depends(get_current_admin_user),
    session: Session = Depends(db_session_dep),
) -> DiscordMenuAuthorsResponse:
    current = load_all_settings(session)
    merged = merge_settings_update(current, body.settings)
    saved = save_settings(session, merged, updated_by_user_id=admin.id)
    return DiscordMenuAuthorsResponse(settings=saved)


@router.get("/api/discord/timeline", response_model=DiscordTimelineEnvelope)
def discord_timeline(
    menu_slot: str = Query(default="twitter_kol"),
    hours: int = Query(default=72, ge=1, le=24 * 30),
    limit: int = Query(default=30, ge=1, le=200),
    ticker: str | None = Query(default=None),
    authors: str | None = Query(default=None, description="Comma-separated author filter"),
    before_timestamp: str | None = Query(default=None),
    locale: str = Query(default="zh", pattern="^(zh|en)$"),
    session: Session = Depends(db_session_dep),
) -> DiscordTimelineEnvelope:
    loc = parse_locale(locale)
    menu_authors = resolve_author_filter(session, menu_slot)
    requested = parse_authors_csv(authors)
    merged_authors = merge_author_filters(menu_authors, requested)
    before_dt = _parse_before_timestamp(before_timestamp)
    authors_token = _authors_cache_token(merged_authors)
    before_token = before_dt.isoformat() if before_dt else "head"
    cache_key = (
        f"discord:timeline:v1:{menu_slot}:{hours}:{limit}:{authors_token}:"
        f"{before_token}:{ticker or 'all'}:{loc}"
    )
    cached = cache_get(cache_key)
    if isinstance(cached, dict):
        return DiscordTimelineEnvelope.model_validate(cached)

    fetch_limit = limit + 1
    rows = list_discord_feed_rows(
        session,
        ticker=ticker,
        hours=hours,
        limit=fetch_limit,
        authors=merged_authors,
        before=before_dt,
    )
    has_more = len(rows) > limit
    page_rows = rows[:limit]

    hub_cache_key = f"discord:kol-hub:v1:{menu_slot}:{hours}:{loc}"
    hub_cached = cache_get(hub_cache_key)
    if isinstance(hub_cached, dict) and isinstance(hub_cached.get("items"), list):
        hub = {
            str(item.get("author")): item
            for item in hub_cached["items"]
            if isinstance(item, dict) and item.get("author")
        }
    else:
        hub_entries = list_kol_hub(session, menu_slot=menu_slot, hours=hours)
        hub = {e.author: e for e in hub_entries}

    items: list[DiscordTimelineItem] = []
    for r in page_rows:
        meta = hub.get(r.author or "")
        if isinstance(meta, dict):
            display_name = meta.get("display_name")
            avatar_url = meta.get("avatar_url")
        elif meta is not None:
            display_name = meta.display_name
            avatar_url = meta.avatar_url
        else:
            display_name = None
            avatar_url = None
        items.append(
            _timeline_item_from_row(
                r,
                display_name=str(display_name) if display_name else None,
                avatar_url=str(avatar_url) if avatar_url else None,
                locale=loc,
            )
        )

    next_before: str | None = None
    if has_more and page_rows:
        last_ts = datetime.fromisoformat(
            page_rows[-1].timestamp_utc_iso.replace("Z", "+00:00")
        )
        next_before = _cursor_timestamp_iso(last_ts)

    response = DiscordTimelineEnvelope(
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
        menu_slot=menu_slot,
        items=items,
        next_before=next_before,
        has_more=has_more,
    )
    cache_set(cache_key, response.model_dump(), ttl=TTL_HOT)
    return response
