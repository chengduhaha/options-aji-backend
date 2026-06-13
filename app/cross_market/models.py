"""Persistent entities for cross-market snapshots (async PostgreSQL)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.cross_market.orm_base import CrossMarketBase


class EventSnapshotRecord(CrossMarketBase):
    __tablename__ = "event_snapshots"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title_zh: Mapped[str] = mapped_column(String(512), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_time: Mapped[str] = mapped_column(String(64), nullable=False)
    options_probability: Mapped[float] = mapped_column(Float, nullable=False)
    polymarket_probability: Mapped[float] = mapped_column(Float, nullable=False)
    social_probability: Mapped[float] = mapped_column(Float, nullable=False)
    institutional_probability: Mapped[float] = mapped_column(Float, nullable=False)
    consensus_probability: Mapped[float] = mapped_column(Float, nullable=False)
    disagreement: Mapped[float] = mapped_column(Float, nullable=False)
    arbitrage_direction: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ArbitrageSignalRecord(CrossMarketBase):
    __tablename__ = "arbitrage_signals"

    signal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    question: Mapped[str] = mapped_column(String(512), nullable=False)
    options_probability: Mapped[float] = mapped_column(Float, nullable=False)
    polymarket_probability: Mapped[float] = mapped_column(Float, nullable=False)
    social_probability: Mapped[float] = mapped_column(Float, nullable=False)
    institutional_probability: Mapped[float] = mapped_column(Float, nullable=False)
    consensus_probability: Mapped[float] = mapped_column(Float, nullable=False)
    disagreement: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    arbitrage_direction: Mapped[str] = mapped_column(String(128), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
