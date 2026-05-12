"""Historical Discord ingest API."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import bearer_subscription_optional
from app.db.session import db_session_dep
from app.ingest.message_store import StoredDiscordMessage, list_messages_recent

router = APIRouter(tags=["messages"])


class DiscordMessagePayload(BaseModel):
    id: str
    channel_id: str
    author: Optional[str]
    content: Optional[str]
    timestamp: str
    tickers: list[str]


class MessagesEnvelope(BaseModel):
    messages: list[DiscordMessagePayload]


def _to_payload(row: StoredDiscordMessage) -> DiscordMessagePayload:
    return DiscordMessagePayload(
        id=row.id,
        channel_id=row.channel_id,
        author=row.author,
        content=row.content,
        timestamp=row.timestamp_utc_iso,
        tickers=list(row.tickers),
    )


@router.get("/api/messages")
def list_recent_messages(
    ticker: Optional[str] = Query(default=None),
    hours: int = Query(default=24, ge=1, le=24 * 21),
    limit: int = Query(default=20, ge=1, le=250),
    session: Session = Depends(db_session_dep),
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> MessagesEnvelope:
    rows = list_messages_recent(session, ticker=ticker, hours=hours, limit=limit)
    return MessagesEnvelope(messages=[_to_payload(r) for r in rows])
