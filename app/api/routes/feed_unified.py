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
from app.ingest.message_store import list_messages_recent

router = APIRouter(tags=["feed"])

FeedKind = Literal["signal", "discord"]


class FeedItem(BaseModel):
    id: str
    kind: FeedKind
    created_at_utc: str
    title: str
    body: str
    tickers: list[str] = Field(default_factory=list)
    sentiment: Optional[str] = None
    priority: Optional[str] = None


class FeedEnvelope(BaseModel):
    generated_at_utc: str
    items: list[FeedItem]


@router.get("/api/feed")
def unified_feed(
    kind: Optional[str] = Query(
        default=None,
        description="Filter: signal | discord | all (default)",
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
        rows = list_messages_recent(
            session,
            ticker=ticker,
            hours=hours,
            limit=limit_discord,
        )
        for r in rows:
            items.append(
                FeedItem(
                    id=f"dc-{r.id}",
                    kind="discord",
                    created_at_utc=r.timestamp_utc_iso,
                    title=r.author or "Discord",
                    body=(r.content or "")[:2000],
                    tickers=list(r.tickers),
                    sentiment=None,
                    priority=None,
                )
            )

    items.sort(key=lambda x: x.created_at_utc, reverse=True)
    return FeedEnvelope(
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
        items=items,
    )
