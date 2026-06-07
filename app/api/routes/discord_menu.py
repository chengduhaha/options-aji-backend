"""Discord menu author whitelist — admin configure, users read."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps_auth import get_current_admin_user, get_current_user
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.ingest.message_store import list_discord_feed_rows
from app.services.discord_menu_authors import (
    DISCORD_MENU_SLOT_LABELS,
    KNOWN_DISCORD_MENU_SLOTS,
    list_distinct_authors,
    load_all_settings,
    merge_settings_update,
    resolve_author_filter,
    save_settings,
)

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
    bullets_zh: list[str] | None = None
    risk_note_zh: str | None = None


class DiscordTimelineEnvelope(BaseModel):
    generated_at_utc: str
    menu_slot: str
    items: list[DiscordTimelineItem]


@router.get("/api/admin/discord/authors", response_model=DiscordAuthorsResponse)
def admin_list_discord_authors(
    session: Session = Depends(db_session_dep),
    _: UserRow = Depends(get_current_admin_user),
) -> DiscordAuthorsResponse:
    from datetime import datetime, timezone

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
    limit: int = Query(default=50, ge=1, le=200),
    ticker: str | None = Query(default=None),
    session: Session = Depends(db_session_dep),
) -> DiscordTimelineEnvelope:
    from datetime import datetime, timezone

    authors = resolve_author_filter(session, menu_slot)
    rows = list_discord_feed_rows(
        session,
        ticker=ticker,
        hours=hours,
        limit=limit,
        authors=authors,
    )
    items: list[DiscordTimelineItem] = []
    for r in rows:
        has_zh = bool(
            (r.enrichment_title_zh or "").strip()
            or (r.enrichment_summary_zh or "").strip()
            or r.enrichment_bullets_zh
        )
        if has_zh:
            title = (r.enrichment_title_zh or "").strip() or (r.author or "Discord")
            body = ((r.enrichment_summary_zh or "").strip() or (r.content or "")[:2000])[:4000]
            bullets = [b for b in r.enrichment_bullets_zh if str(b).strip()] or None
        else:
            title = r.author or "Discord"
            body = (r.content or "")[:2000]
            bullets = None
        items.append(
            DiscordTimelineItem(
                id=f"dc-{r.id}",
                created_at_utc=r.timestamp_utc_iso,
                title=title,
                body=body,
                tickers=list(r.tickers),
                author=r.author,
                raw_body=r.content,
                bullets_zh=bullets,
                risk_note_zh=(r.enrichment_risk_zh or "").strip() or None,
            )
        )
    return DiscordTimelineEnvelope(
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
        menu_slot=menu_slot,
        items=items,
    )
