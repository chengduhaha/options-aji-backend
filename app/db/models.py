"""SQLAlchemy ORM models — supports both SQLite (dev) and PostgreSQL (prod)."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint, JSON, BigInteger, Boolean, Date, DateTime, Float, ForeignKey,
    Index, Integer, Numeric, String, Text, func, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ─── Discord / Feed ───────────────────────────────────────────────────────────

class DiscordMessageRow(Base):
    __tablename__ = "discord_messages"
    __table_args__ = (
        Index("idx_discord_messages_timestamp", "timestamp"),
        Index("idx_discord_messages_author_timestamp", "author", "timestamp"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    author: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tickers: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


class MessageEnrichmentRow(Base):
    __tablename__ = "message_enrichment"
    __table_args__ = (Index("idx_message_enrichment_created", "created_at"),)

    message_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("discord_messages.id", ondelete="CASCADE"), primary_key=True
    )
    language_detected: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    title_zh: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    summary_zh: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bullets_zh: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    risk_note_zh: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    title_en: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    summary_en: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bullets_en: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    risk_note_en: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    enrichment_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


# ─── Billing / Auth ───────────────────────────────────────────────────────────

class StripeWebhookEventRow(Base):
    __tablename__ = "stripe_webhook_events"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    received_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


class PaymentWebhookEventRow(Base):
    __tablename__ = "payment_webhook_events"
    __table_args__ = (Index("idx_payment_webhook_provider_received", "provider", "received_at"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    received_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


class ApiEntitlementRow(Base):
    __tablename__ = "api_entitlements"
    __table_args__ = (
        Index("idx_api_entitlements_customer", "stripe_customer_id"),
        Index("idx_api_entitlements_user_provider", "user_id", "provider"),
        Index("idx_api_entitlements_provider_customer", "provider", "provider_customer_id"),
        Index("idx_api_entitlements_provider_subscription", "provider", "provider_subscription_id"),
    )

    api_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    plan: Mapped[str] = mapped_column(String(32), nullable=False, default="free")
    current_period_end: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    provider_customer_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    provider_subscription_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    provider_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    provider_price_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    past_due_since: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True
    )


class UsageDailyRow(Base):
    __tablename__ = "usage_daily"
    __table_args__ = (Index("idx_usage_daily_key_day", "api_key", "usage_date"),)

    api_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    usage_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    agent_queries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AccessKeyRow(Base):
    """Manual trial/paid access keys for early-stage MVP entitlement."""

    __tablename__ = "access_keys"
    __table_args__ = (
        Index("idx_access_keys_prefix", "key_prefix"),
        Index("idx_access_keys_status_exp", "status", "expires_at"),
    )

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    key_type: Mapped[str] = mapped_column(String(16), nullable=False, default="trial")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    bound_user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    bound_email: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    bound_device_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    max_devices: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True
    )


class LlmUsageRow(Base):
    """Persisted LLM token/cost usage for admin cost monitoring."""

    __tablename__ = "llm_usage"
    __table_args__ = (
        Index("idx_llm_usage_created_provider", "created_at", "provider"),
        Index("idx_llm_usage_source", "source"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    source: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


# ─── Reference Data (low-freq) ────────────────────────────────────────────────

class OptionsContractRow(Base):
    """Massive /v3/reference/options/contracts — option contract metadata."""
    __tablename__ = "options_contracts"
    __table_args__ = (
        Index("idx_oc_underlying_exp", "underlying_ticker", "expiration_date"),
        Index("idx_oc_underlying_exp_strike", "underlying_ticker", "expiration_date", "strike_price"),
    )

    ticker: Mapped[str] = mapped_column(String(64), primary_key=True)
    underlying_ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    contract_type: Mapped[str] = mapped_column(String(8), nullable=False)
    exercise_style: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    expiration_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    strike_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    shares_per_contract: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    primary_exchange: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    is_expired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CompanyProfileRow(Base):
    """FMP /stable/profile — company fundamentals."""
    __tablename__ = "company_profiles"
    __table_args__ = (Index("idx_cp_sector_industry", "sector", "industry"),)

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    company_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    industry: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    sector: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ceo: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    employees: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    website: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    image_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    ipo_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    market_cap: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    is_etf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exchange: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    raw_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ─── Market Snapshots (mid-freq) ─────────────────────────────────────────────

class StockQuoteRow(Base):
    """FMP /stable/quote — latest stock quote (upserted on each sync)."""
    __tablename__ = "stock_quotes"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    change: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    change_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    day_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    day_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    year_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    year_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volume: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    avg_volume: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    market_cap: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    pe: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    eps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    open_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    previous_close: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    snapshot_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OptionsSnapshotRow(Base):
    """Massive /v3/snapshot/options — latest option contract snapshot (upserted)."""
    __tablename__ = "options_snapshots"
    __table_args__ = (
        Index("idx_os_underlying_exp", "underlying_ticker", "expiration_date"),
    )

    ticker: Mapped[str] = mapped_column(String(64), primary_key=True)
    underlying_ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    contract_type: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    expiration_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    strike_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    delta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gamma: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    theta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vega: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    implied_volatility: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    open_interest: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    bid: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ask: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bid_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ask_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    midpoint: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    day_open: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    day_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    day_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    day_close: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    day_volume: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    day_vwap: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    day_change: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    day_change_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    previous_close: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    break_even_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    underlying_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_trade_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_trade_size: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_trade_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    snapshot_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SiteNavSettingsRow(Base):
    """Global sidebar visibility — admin toggles, applies to non-admin users."""

    __tablename__ = "site_nav_settings"

    key: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    visibility: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by_user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)


class DiscordMenuAuthorSettingsRow(Base):
    """Per-menu Discord author whitelist — empty list means no filter (show all)."""

    __tablename__ = "discord_menu_author_settings"

    menu_slot: Mapped[str] = mapped_column(String(64), primary_key=True)
    allowed_authors: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by_user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)


class DiscordAuthorProfileRow(Base):
    """Display profile for a Discord/TweetShift author (avatar, bio, handle)."""

    __tablename__ = "discord_author_profiles"

    author: Mapped[str] = mapped_column(String(256), primary_key=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    avatar_filename: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    bio_zh: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bio_en: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    twitter_handle: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by_user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)


# ─── Supply Chain Graph ───────────────────────────────────────────────────────

class GraphNodeRow(Base):
    """Generic graph node for company / segment / industry / product entities."""

    __tablename__ = "graph_nodes"
    __table_args__ = (
        CheckConstraint(
            "node_type IN ('company', 'segment', 'industry', 'product')",
            name="ck_graph_nodes_node_type",
        ),
        Index("idx_graph_nodes_node_type", "node_type"),
        Index("idx_graph_nodes_sector", "sector"),
        Index(
            "uq_graph_nodes_ticker_market",
            "ticker",
            "market",
            unique=True,
            sqlite_where=text("ticker IS NOT NULL"),
            postgresql_where=text("ticker IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    node_type: Mapped[str] = mapped_column(String(24), nullable=False)
    ticker: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    market: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    name_zh: Mapped[str] = mapped_column(Text, nullable=False)
    name_en: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sector: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    is_listed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    logo_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attrs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True
    )


class GraphEdgeRow(Base):
    """Generic graph edge for supply-chain, investment, segment and thematic links."""

    __tablename__ = "graph_edges"
    __table_args__ = (
        CheckConstraint(
            "rel_type IN ('supplies_to', 'mutual_supply', 'invests_in', 'parent_of', "
            "'has_segment', 'joint_development', 'partnership', 'competitor', "
            "'licenses_to', 'manufactures_for', 'thematic_link')",
            name="ck_graph_edges_rel_type",
        ),
        CheckConstraint(
            "direction IN ('directed', 'bidirectional', 'undirected')",
            name="ck_graph_edges_direction",
        ),
        CheckConstraint(
            "moat_tier IS NULL OR moat_tier IN ('exclusive', 'primary', 'dominant', 'scarce', 'normal')",
            name="ck_graph_edges_moat_tier",
        ),
        CheckConstraint(
            "confidence IN ('confirmed', 'inferred')",
            name="ck_graph_edges_confidence",
        ),
        Index("idx_graph_edges_source_id", "source_id"),
        Index("idx_graph_edges_target_id", "target_id"),
        Index("idx_graph_edges_rel_type", "rel_type"),
        Index("idx_graph_edges_moat_tier", "moat_tier"),
        Index(
            "uq_graph_edges_identity",
            "source_id",
            "target_id",
            "rel_type",
            "label",
            "as_of_date",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_id: Mapped[str] = mapped_column(String(36), ForeignKey("graph_nodes.id", ondelete="CASCADE"), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), ForeignKey("graph_nodes.id", ondelete="CASCADE"), nullable=False)
    rel_type: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="directed")
    label: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    semantic: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    moat_tier: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    weight: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    attrs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="confirmed")
    evidence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    as_of_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True
    )


class GraphViewRow(Base):
    """Curated saved graph views such as SpaceX full-business supply chain."""

    __tablename__ = "graph_views"
    __table_args__ = (
        Index("idx_graph_views_slug", "slug", unique=True),
        Index("idx_graph_views_perspective", "perspective"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    slug: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    perspective: Mapped[str] = mapped_column(String(32), nullable=False)
    focus_node_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("graph_nodes.id", ondelete="SET NULL"), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True
    )


# ─── Historical Bars (time-series) ───────────────────────────────────────────

class StockDailyBarRow(Base):
    """FMP /stable/historical-price-eod — daily OHLCV."""
    __tablename__ = "stock_daily_bars"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    bar_date: Mapped[datetime] = mapped_column(Date, primary_key=True)
    open_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    close: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    adj_close: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volume: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)


# ─── Financial Data ───────────────────────────────────────────────────────────

class EarningsCalendarRow(Base):
    """FMP /stable/earnings-calendar — upcoming and historical earnings."""
    __tablename__ = "earnings_calendar"
    __table_args__ = (
        Index("idx_ec_date", "earnings_date"),
    )

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    earnings_date: Mapped[datetime] = mapped_column(Date, primary_key=True)
    eps_estimate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    eps_actual: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    revenue_estimate: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    revenue_actual: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    surprise_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    time: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    is_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


# ─── Market Intelligence ──────────────────────────────────────────────────────

class StockNewsRow(Base):
    """FMP /stable/news/stock — per-symbol news."""
    __tablename__ = "stock_news"
    __table_args__ = (Index("idx_news_published", "published_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbols: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    title_zh: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary_zh: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AnalystRatingRow(Base):
    """FMP /stable/stock-grades — analyst rating changes."""
    __tablename__ = "analyst_ratings"
    __table_args__ = (Index("idx_ar_symbol_date", "symbol", "rating_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    analyst_company: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    rating_action: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    rating_from: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    rating_to: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    price_target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rating_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class SectorPerformanceRow(Base):
    """FMP /stable/sector-performance — daily sector snapshot."""
    __tablename__ = "sector_performance"

    snapshot_date: Mapped[datetime] = mapped_column(Date, primary_key=True)
    sector: Mapped[str] = mapped_column(String(128), primary_key=True)
    change_pct_1d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pe_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class MacroCalendarRow(Base):
    """FMP /stable/economic-calendar — macro events."""
    __tablename__ = "macro_calendar"
    __table_args__ = (Index("idx_mc_event_date", "event_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    country: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    event_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    impact: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    estimate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    previous: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    actual: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    event_name_zh: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class TreasuryRateRow(Base):
    """FMP /stable/treasury-rates — yield curve."""
    __tablename__ = "treasury_rates"

    rate_date: Mapped[datetime] = mapped_column(Date, primary_key=True)
    month1: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    month2: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    month3: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    month6: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    year1: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    year2: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    year5: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    year10: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    year30: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class EtfHoldingRow(Base):
    """FMP /stable/etf-holdings — ETF portfolio."""
    __tablename__ = "etf_holdings"
    __table_args__ = (Index("idx_eh_etf_date", "etf_symbol", "filing_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    etf_symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    filing_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    holding_symbol: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    holding_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    weight_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    shares: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    market_value: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


# ─── User Data ────────────────────────────────────────────────────────────────

class WatchlistRow(Base):
    """User watchlist — symbols to track per API key."""
    __tablename__ = "watchlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api_key: Mapped[str] = mapped_column(String(256), nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ─── Social / Resonance ───────────────────────────────────────────────────────

class SocialPostRow(Base):
    __tablename__ = "social_posts"
    __table_args__ = (
        Index("idx_social_posts_created", "created_at"),
        Index("idx_social_posts_source", "source"),
        Index("uq_social_posts_source_external", "source", "external_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    author: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    comments_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tickers: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    raw_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TickerSentimentSnapshotRow(Base):
    __tablename__ = "ticker_sentiment_snapshots"
    __table_args__ = (
        Index("idx_ticker_sentiment_symbol_time", "symbol", "snapshot_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sentiment_score: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="neutral")
    mention_count_24h: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mention_growth_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source_breakdown: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ResonanceSignalRow(Base):
    __tablename__ = "resonance_signals"
    __table_args__ = (Index("idx_resonance_symbol_triggered", "symbol", "triggered_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False, default="resonance")
    institutional_direction: Mapped[str] = mapped_column(String(16), nullable=False, default="neutral")
    retail_direction: Mapped[str] = mapped_column(String(16), nullable=False, default="neutral")
    institutional_strength: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retail_strength: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    narrative_zh: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    narrative_en: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserAlertRow(Base):
    __tablename__ = "user_alerts"
    __table_args__ = (Index("idx_user_alerts_api_key_symbol", "api_key", "symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api_key: Mapped[str] = mapped_column(String(256), nullable=False)
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    threshold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserPushSettingRow(Base):
    __tablename__ = "user_push_settings"
    __table_args__ = (Index("idx_user_push_settings_api_key", "api_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    push_discord: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    push_telegram: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    push_email: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    keywords: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UserScannerTemplateRow(Base):
    __tablename__ = "user_scanner_templates"
    __table_args__ = (
        Index("idx_user_scanner_templates_api_key", "api_key"),
        Index("idx_user_scanner_templates_api_key_name", "api_key", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api_key: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ─── V3 Alternative Data ──────────────────────────────────────────────────────

class RetailInsiderDivergenceRow(Base):
    """Cached divergence scan results — retail FOMO vs insider sales."""
    __tablename__ = "retail_insider_divergence"
    __table_args__ = (
        Index("idx_rid_symbol_time", "symbol", "scanned_at"),
        Index("idx_rid_alert", "alert_level", "scanned_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    social_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mention_growth_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    insider_sell_usd: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    divergence_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    alert_level: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    ai_narrative_zh: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    insider_trades_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DarkPoolTradeRow(Base):
    """Dark pool block trades (FINRA TRF / OTC)."""
    __tablename__ = "dark_pool_trades"
    __table_args__ = (
        Index("idx_dpt_symbol_time", "symbol", "trade_time"),
        Index("idx_dpt_time", "trade_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notional_value: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    direction: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    exchange: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    raw_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MarketTideDailyRow(Base):
    """Daily options market tide — call vs put premium flow."""
    __tablename__ = "market_tide_daily"

    trade_date: Mapped[datetime] = mapped_column(Date, primary_key=True)
    call_premium_total: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    put_premium_total: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    net_call_flow: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    call_volume: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    put_volume: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    put_call_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tide_direction: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CongressTradeRow(Base):
    """US Congress member trade disclosures (STOCK Act)."""
    __tablename__ = "congress_trades"
    __table_args__ = (
        Index("idx_ct_member_symbol", "member_name", "symbol"),
        Index("idx_ct_symbol_date", "symbol", "trade_date"),
        Index("idx_ct_trade_date", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    member_name: Mapped[str] = mapped_column(String(256), nullable=False)
    chamber: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    trade_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    transaction_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    amount_range: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    asset_description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
