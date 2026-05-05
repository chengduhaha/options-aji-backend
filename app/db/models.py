"""SQLAlchemy ORM models — supports both SQLite (dev) and PostgreSQL (prod)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON, BigInteger, Boolean, Date, DateTime, Float, ForeignKey,
    Index, Integer, Numeric, String, Text, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ─── Discord / Feed ───────────────────────────────────────────────────────────

class DiscordMessageRow(Base):
    __tablename__ = "discord_messages"
    __table_args__ = (Index("idx_discord_messages_timestamp", "timestamp"),)

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


class ApiEntitlementRow(Base):
    __tablename__ = "api_entitlements"
    __table_args__ = (Index("idx_api_entitlements_customer", "stripe_customer_id"),)

    api_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    plan: Mapped[str] = mapped_column(String(32), nullable=False, default="free")
    current_period_end: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class UsageDailyRow(Base):
    __tablename__ = "usage_daily"
    __table_args__ = (Index("idx_usage_daily_key_day", "api_key", "usage_date"),)

    api_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    usage_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    agent_queries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


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
    contract_type: Mapped[str] = mapped_column(String(8), nullable=False)  # call/put
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
    # Greeks
    delta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gamma: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    theta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vega: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    implied_volatility: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    open_interest: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Quote
    bid: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ask: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bid_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ask_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    midpoint: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Day bar
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
    snapshot_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
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
    time: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)  # BMO/AMC
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


class InsiderTradeRow(Base):
    """FMP /stable/insider-trading."""
    __tablename__ = "insider_trades"
    __table_args__ = (Index("idx_it_symbol_date", "symbol", "transaction_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    filer_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    filer_relation: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    transaction_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    transaction_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    shares: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    price_per_share: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_value: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    shares_owned_after: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    filing_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class CongressTradeRow(Base):
    """FMP /stable/senate-latest-trading and /stable/house-latest-trading."""
    __tablename__ = "congress_trades"
    __table_args__ = (
        Index("idx_ct_symbol_date", "symbol", "transaction_date"),
        Index("idx_ct_date", "transaction_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chamber: Mapped[str] = mapped_column(String(8), nullable=False)  # senate/house
    member_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    symbol: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    asset_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    transaction_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    transaction_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    amount_range: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    filing_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    raw_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
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
