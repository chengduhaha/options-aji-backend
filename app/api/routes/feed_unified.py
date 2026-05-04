"""Unified intelligence feed: signals + Discord archive."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import bearer_subscription_optional
from app.api.routes.signals_feed import SignalCard, signals_feed
from app.db.session import db_session_dep
from app.ingest.intel_macro import (
    fetch_macro_calendar_rows,
    macro_row_stable_id,
    macro_row_timestamp_iso,
)
from app.ingest.message_store import list_discord_feed_rows

router = APIRouter(tags=["feed"])

FeedKind = Literal["signal", "discord", "macro", "twitter"]


class FeedItem(BaseModel):
    id: str
    kind: FeedKind
    created_at_utc: str
    title: str
    body: str
    tickers: list[str] = Field(default_factory=list)
    sentiment: Optional[str] = None
    priority: Optional[str] = None
    #: Original Discord text (for “expand”); signals leave unset.
    raw_body: Optional[str] = None
    original_lang: Optional[str] = None
    bullets_zh: Optional[list[str]] = Field(default=None)
    risk_note_zh: Optional[str] = None


class FeedEnvelope(BaseModel):
    generated_at_utc: str
    items: list[FeedItem]


def _discord_feed_item(
    *,
    r_id: str,
    created: str,
    author: Optional[str],
    content: Optional[str],
    tickers: list[str],
    enrichment_title_zh: Optional[str],
    enrichment_summary_zh: Optional[str],
    enrichment_bullets_zh: tuple[str, ...],
    enrichment_risk_zh: Optional[str],
    enrichment_lang: Optional[str],
) -> FeedItem:
    has_zh = bool(
        (enrichment_title_zh or "").strip()
        or (enrichment_summary_zh or "").strip()
        or enrichment_bullets_zh,
    )
    if has_zh:
        title = (enrichment_title_zh or "").strip() or (author or "Discord")
        body = ((enrichment_summary_zh or "").strip() or (content or "")[:2000])[:4000]
        bullets_list = [b for b in enrichment_bullets_zh if str(b).strip()]
        return FeedItem(
            id=f"dc-{r_id}",
            kind="discord",
            created_at_utc=created,
            title=title,
            body=body,
            tickers=list(tickers),
            raw_body=content,
            original_lang=enrichment_lang,
            bullets_zh=bullets_list or None,
            risk_note_zh=(enrichment_risk_zh.strip() if (enrichment_risk_zh or "").strip() else None),
        )

    plain = content or ""
    return FeedItem(
        id=f"dc-{r_id}",
        kind="discord",
        created_at_utc=created,
        title=author or "Discord",
        body=plain[:2000],
        tickers=list(tickers),
    )


@router.get("/api/feed")
def unified_feed(
    kind: Optional[str] = Query(
        default=None,
        description="Filter: signal | discord | macro | twitter | all (default)",
    ),
    ticker: Optional[str] = Query(default=None),
    hours: int = Query(default=72, ge=1, le=24 * 30),
    limit_signals: int = Query(default=40, ge=1, le=200),
    limit_discord: int = Query(default=40, ge=1, le=200),
    session: Session = Depends(db_session_dep),
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> FeedEnvelope:
    items: list[FeedItem] = []
    want_signals = kind in (None, "all", "signal")
    want_discord = kind in (None, "all", "discord")
    want_macro = kind in (None, "all", "macro")
    want_twitter = kind in (None, "all", "twitter")

    if want_signals:
        env = signals_feed(_)
        sigs: list[SignalCard] = env.signals[:limit_signals]
        for s in sigs:
            if ticker and s.ticker.upper() != ticker.strip().upper():
                continue
            items.append(
                FeedItem(
                    id=f"sig-{s.id}",
                    kind="signal",
                    created_at_utc=env.generated_at_utc,
                    title=s.title,
                    body=s.summary,
                    tickers=[s.ticker],
                    sentiment=s.direction,
                    priority=s.priority,
                )
            )

    if want_discord:
        rows = list_discord_feed_rows(
            session,
            ticker=ticker,
            hours=hours,
            limit=limit_discord,
        )
        for r in rows:
            items.append(
                _discord_feed_item(
                    r_id=r.id,
                    created=r.timestamp_utc_iso,
                    author=r.author,
                    content=r.content,
                    tickers=r.tickers,
                    enrichment_title_zh=r.enrichment_title_zh,
                    enrichment_summary_zh=r.enrichment_summary_zh,
                    enrichment_bullets_zh=r.enrichment_bullets_zh,
                    enrichment_risk_zh=r.enrichment_risk_zh,
                    enrichment_lang=r.enrichment_lang,
                )
            )

    if want_macro:
        for row in fetch_macro_calendar_rows(limit=60):
            eid = macro_row_stable_id(row)
            event = str(row.get("event") or "宏观事件")
            country = str(row.get("country") or "")
            impact = str(row.get("impact") or "")
            est = row.get("estimate")
            prev = row.get("previous")
            body_parts = [p for p in (country, impact) if p]
            if est is not None:
                body_parts.append(f"预期 {est}")
            if prev is not None:
                body_parts.append(f"前值 {prev}")
            items.append(
                FeedItem(
                    id=eid,
                    kind="macro",
                    created_at_utc=macro_row_timestamp_iso(row),
                    title=event[:500],
                    body=" · ".join(body_parts) or "宏观日历",
                ),
            )

    if want_twitter:
        # 付费 X / 聚合源：配置 TWITTER_API_IO_KEY 后可接入；当前返回 0 条占位。
        pass

    items.sort(key=lambda x: x.created_at_utc, reverse=True)
    return FeedEnvelope(
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
        items=items,
    )
