"""SQLAlchemy models for discord message persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Optional


from sqlalchemy import JSON, Boolean, DateTime, Index, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DiscordMessageRow(Base):
    __tablename__ = "discord_messages"
    __table_args__ = (Index("idx_discord_messages_timestamp", "timestamp"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    author: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tickers: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
