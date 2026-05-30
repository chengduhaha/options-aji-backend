"""Async SQLAlchemy engine for ontology persistence (PostgreSQL only)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
SessionLocal: async_sessionmaker[AsyncSession] | None = None


def _to_async_pg_url(url: str) -> str | None:
    u = url.strip()
    if not u.startswith("postgresql"):
        return None
    u = u.replace("postgresql+psycopg2://", "postgresql://", 1)
    if u.startswith("postgresql://"):
        return u.replace("postgresql://", "postgresql+asyncpg://", 1)
    return None


def init_ontology_async_db() -> None:
    """Initialize async engine when DATABASE_URL is PostgreSQL."""
    global _engine, SessionLocal
    settings = get_settings()
    async_url = _to_async_pg_url(settings.database_url)
    if not async_url:
        logger.info("ontology persistence: skipping async DB (use PostgreSQL for traces/snapshots)")
        _engine = None
        SessionLocal = None
        return
    _engine = create_async_engine(async_url, echo=False, pool_pre_ping=True)
    SessionLocal = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def create_ontology_tables() -> None:
    if _engine is None:
        return
    from app.cross_market.models import (  # noqa: PLC0415
        AgentTraceRecord,
        ArbitrageSignalRecord,
        EventSnapshotRecord,
    )
    from app.cross_market.orm_base import OntologyBase

    _ = (AgentTraceRecord, ArbitrageSignalRecord, EventSnapshotRecord)
    async with _engine.begin() as connection:
        await connection.run_sync(OntologyBase.metadata.create_all)
