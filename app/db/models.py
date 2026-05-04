"""SQLAlchemy models for discord message persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Optional


from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
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


class StripeWebhookEventRow(Base):
    """Stripe event id dedup (webhook idempotency)."""

    __tablename__ = "stripe_webhook_events"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    received_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


class ApiEntitlementRow(Base):
    """Maps a client API key (Bearer) to Stripe customer + plan tier."""

    __tablename__ = "api_entitlements"
    __table_args__ = (Index("idx_api_entitlements_customer", "stripe_customer_id"),)

    api_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    plan: Mapped[str] = mapped_column(String(32), nullable=False, default="free")
    current_period_end: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class UsageDailyRow(Base):
    """Per-key per-UTC-day counters (e.g. AI agent calls)."""

    __tablename__ = "usage_daily"
    __table_args__ = (Index("idx_usage_daily_key_day", "api_key", "usage_date"),)

    api_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    usage_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    agent_queries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class MessageEnrichmentRow(Base):
    """LLM-generated Chinese insight for a Discord message."""

    __tablename__ = "message_enrichment"
    __table_args__ = (Index("idx_message_enrichment_created", "created_at"),)

    message_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("discord_messages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    language_detected: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    title_zh: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    summary_zh: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bullets_zh: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    risk_note_zh: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
