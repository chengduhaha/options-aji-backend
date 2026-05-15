"""Unified intelligence feed: signals + Discord archive."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import bearer_subscription_optional
from app.api.routes.signals_feed import SignalCard, signals_feed
from app.db.models import SocialPostRow, StockNewsRow
from app.db.session import db_session_dep
from app.ingest.intel_macro import (
    fetch_macro_calendar_rows,
    macro_row_stable_id,
    macro_row_timestamp_iso,
)
from app.ingest.message_store import list_discord_feed_rows
from app.services.resonance_feed import resonance_stream_to_feed_fields, social_row_matches_kol_filter
from app.services.social_sentiment import (
    ResonanceStreamItem,
    kol_handle_set_from_settings,
    list_resonance_timeline,
)

router = APIRouter(tags=["feed"])

FeedKind = Literal["signal", "discord", "macro", "twitter", "news", "resonance"]


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


def _feed_item_from_resonance_row(ritem: ResonanceStreamItem) -> FeedItem:
    f = resonance_stream_to_feed_fields(ritem)
    return FeedItem(
        id=f.id,
        kind="resonance",
        created_at_utc=f.created_at_utc,
        title=f.title,
        body=f.body,
        tickers=list(f.tickers),
        sentiment=f.sentiment,
        priority=f.priority,
    )


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
        description="Filter: signal | discord | macro | twitter | resonance | all (default)",
    ),
    ticker: Optional[str] = Query(default=None),
    hours: int = Query(default=72, ge=1, le=24 * 30),
    limit_signals: int = Query(default=40, ge=1, le=200),
    limit_discord: int = Query(default=40, ge=1, le=200),
    kol_only: bool = Query(
        default=False,
        description="When true, only include social posts from configured KOL handles.",
    ),
    session: Session = Depends(db_session_dep),
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> FeedEnvelope:
    items: list[FeedItem] = []
    want_signals = kind in (None, "all", "signal")
    want_discord = kind in (None, "all", "discord")
    want_macro = kind in (None, "all", "macro")
    want_twitter = kind in (None, "all", "twitter")
    want_resonance = kind in (None, "all", "resonance")

    kol_set = kol_handle_set_from_settings()
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
        social_rows = session.execute(
            select(SocialPostRow).order_by(SocialPostRow.created_at.desc()).limit(120)
        ).scalars().all()
        for sr in social_rows:
            if not social_row_matches_kol_filter(sr=sr, kol_only=kol_only, kol_set=kol_set):
                continue
            ticks = [str(t).strip().upper() for t in (sr.tickers or []) if t]
            if ticker and ticker.strip().upper() not in ticks:
                continue
            created = sr.created_at.astimezone(timezone.utc).isoformat()
            title = (sr.title or "").strip() or (sr.author or "Social")
            body = ((sr.content or "").strip() or title)[:4000]
            source_label = "X" if sr.source == "twitter" else "Reddit"
            kol_badge = ""
            raw = sr.raw_json if isinstance(sr.raw_json, dict) else {}
            kh = str(raw.get("kol_handle") or "").strip()
            if kh or bool(raw.get("kol_tracked")):
                kol_badge = "[KOL] "
            items.append(
                FeedItem(
                    id=f"social-{sr.source}-{sr.external_id}",
                    kind="twitter",
                    created_at_utc=created,
                    title=f"{kol_badge}[{source_label}] {title[:480]}",
                    body=body,
                    tickers=ticks[:24],
                    raw_body=sr.content,
                )
            )

    if want_resonance:
        res = list_resonance_timeline(limit=40, symbol=ticker)
        for ritem in res.items:
            items.append(_feed_item_from_resonance_row(ritem))

    items.sort(key=lambda x: x.created_at_utc, reverse=True)
    return FeedEnvelope(
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
        items=items,
    )


@router.get("/api/feed/unified")
def unified_feed_timeline(
    ticker: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(
        default=None,
        description="Filter by kind: signal|discord|macro|news|twitter|resonance",
    ),
    sentiment: Optional[str] = Query(default=None, description="Filter sentiment: bullish|bearish|neutral"),
    priority: Optional[str] = Query(default=None, description="Filter priority value"),
    limit: int = Query(default=50, ge=1, le=200),
    kol_only: bool = Query(
        default=False,
        description="When true, only include social posts from configured KOL handles.",
    ),
    before_timestamp: Optional[str] = Query(
        default=None,
        description="ISO timestamp — items strictly older than this (UTC).",
    ),
    hours: int = Query(default=72, ge=1, le=24 * 30),
    session: Session = Depends(db_session_dep),
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> FeedEnvelope:
    """Time-ordered fusion: Discord + AI signals + macro + stock news."""

    cutoff: Optional[datetime] = None
    if before_timestamp:
        bt = before_timestamp.strip().replace("Z", "+00:00")
        try:
            cutoff = datetime.fromisoformat(bt)
        except ValueError:
            cutoff = None

    tk_up = ticker.strip().upper() if ticker else ""
    kind_filter = kind.strip().lower() if kind else ""
    sentiment_filter = sentiment.strip().lower() if sentiment else ""
    priority_filter = priority.strip().lower() if priority else ""

    kol_set = kol_handle_set_from_settings()

    items: list[FeedItem] = []

    env = signals_feed(_)
    sigs: list[SignalCard] = env.signals[:60]
    for s in sigs:
        if tk_up and s.ticker.upper() != tk_up:
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

    rows_dc = list_discord_feed_rows(
        session,
        ticker=ticker,
        hours=hours,
        limit=80,
    )
    for r in rows_dc:
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

    for row in fetch_macro_calendar_rows(limit=45):
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
        created = macro_row_timestamp_iso(row)
        items.append(
            FeedItem(
                id=eid,
                kind="macro",
                created_at_utc=created,
                title=event[:500],
                body=" · ".join(body_parts) or "宏观日历",
            )
        )

    news_rows = session.execute(
        select(StockNewsRow).order_by(StockNewsRow.published_at.desc()).limit(200)
    ).scalars().all()
    for nr in news_rows:
        syms = [str(s).strip().upper() for s in (nr.symbols or []) if s]
        if tk_up and tk_up not in syms:
            continue
        created = nr.published_at.astimezone(timezone.utc).isoformat()
        title = (nr.title_zh or "").strip() or nr.title
        snippet = (
            ((nr.summary_zh or "") or "").strip()
            or ((nr.content or "") or "").strip()
        )[:4000]
        src = nr.source or "FMP"
        body_blob = snippet or title
        items.append(
            FeedItem(
                id=f"news-{nr.id}",
                kind="news",
                created_at_utc=created,
                title=title[:512],
                body=f"[{src}] · {body_blob}" if body_blob else f"[{src}]",
                tickers=list(syms[:24]),
            )
        )

    social_rows = session.execute(
        select(SocialPostRow).order_by(SocialPostRow.created_at.desc()).limit(200)
    ).scalars().all()
    for sr in social_rows:
        if not social_row_matches_kol_filter(sr=sr, kol_only=kol_only, kol_set=kol_set):
            continue
        ticks = [str(t).strip().upper() for t in (sr.tickers or []) if t]
        if tk_up and tk_up not in ticks:
            continue
        created = sr.created_at.astimezone(timezone.utc).isoformat()
        source_label = "X" if sr.source == "twitter" else "Reddit"
        title = (sr.title or "").strip() or (sr.author or source_label)
        body = ((sr.content or "").strip() or title)[:4000]
        raw = sr.raw_json if isinstance(sr.raw_json, dict) else {}
        kh = str(raw.get("kol_handle") or "").strip()
        kol_badge = ""
        if kh or bool(raw.get("kol_tracked")):
            kol_badge = "[KOL] "
        items.append(
            FeedItem(
                id=f"social-{sr.source}-{sr.external_id}",
                kind="twitter",
                created_at_utc=created,
                title=f"{kol_badge}[{source_label}] {title[:480]}",
                body=body,
                tickers=ticks[:24],
                raw_body=sr.content,
            )
        )

    res_block = list_resonance_timeline(limit=48, symbol=ticker)
    for ritem in res_block.items:
        items.append(_feed_item_from_resonance_row(ritem))

    if cutoff:
        cutoff_utc = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=timezone.utc)
        filtered_items: list[FeedItem] = []
        for it in items:
            try:
                raw_ts = it.created_at_utc.replace("Z", "+00:00")
                ts_it = datetime.fromisoformat(raw_ts)
                if ts_it.tzinfo is None:
                    ts_it = ts_it.replace(tzinfo=timezone.utc)
                if ts_it < cutoff_utc:
                    filtered_items.append(it)
            except ValueError:
                filtered_items.append(it)
        items = filtered_items

    items.sort(key=lambda x: x.created_at_utc, reverse=True)
    if kind_filter:
        items = [item for item in items if item.kind.lower() == kind_filter]
    if sentiment_filter:
        items = [
            item
            for item in items
            if (item.sentiment or "").strip().lower() == sentiment_filter
        ]
    if priority_filter:
        items = [
            item
            for item in items
            if (item.priority or "").strip().lower() == priority_filter
        ]
    items = items[:limit]

    return FeedEnvelope(
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
        items=items,
    )
