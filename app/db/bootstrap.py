"""Database table initialization."""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from app.db.models import Base
from app.db.models_user import UserRow  # noqa: F401
from app.db.session import SessionLocal, engine
from app.services.site_nav import seed_nav_settings

logger = logging.getLogger(__name__)

_OPTIONS_SNAPSHOT_NEW_COLUMNS = (
    ("last_trade_price", "FLOAT"),
    ("last_trade_size", "FLOAT"),
    ("last_trade_at", "TIMESTAMP WITH TIME ZONE"),
)


def _migrate_options_snapshot_columns() -> None:
    """Add last_trade columns to existing options_snapshots tables (idempotent)."""
    insp = inspect(engine)
    if "options_snapshots" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("options_snapshots")}
    dialect = engine.dialect.name
    with engine.begin() as conn:
        for col_name, col_type in _OPTIONS_SNAPSHOT_NEW_COLUMNS:
            if col_name in existing:
                continue
            if dialect == "sqlite":
                sql_type = "REAL" if "FLOAT" in col_type else "TEXT"
            else:
                sql_type = col_type
            conn.execute(
                text(f"ALTER TABLE options_snapshots ADD COLUMN {col_name} {sql_type}")
            )
            logger.info("Added column options_snapshots.%s", col_name)


def init_db() -> None:
    """Create all tables if they don't exist. Safe to call multiple times."""
    logger.info("Initializing database tables...")
    Base.metadata.create_all(bind=engine)
    _migrate_options_snapshot_columns()
    session = SessionLocal()
    try:
        seed_nav_settings(session)
    finally:
        session.close()
    logger.info("Database tables ready.")
