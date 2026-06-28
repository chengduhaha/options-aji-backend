"""Database table initialization."""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from app.db.models import Base
from app.db.models_user import ActivationCodeRow, UserRow  # noqa: F401
from app.db import models_blog  # noqa: F401
from app.db.session import SessionLocal, engine
from app.services.discord_menu_authors import seed_discord_menu_author_settings
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


def _migrate_user_membership_column() -> None:
    """Add membership_expires_at to users table (idempotent)."""
    insp = inspect(engine)
    if "users" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("users")}
    if "membership_expires_at" in existing:
        return
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "sqlite":
            conn.execute(text("ALTER TABLE users ADD COLUMN membership_expires_at TEXT"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN membership_expires_at TIMESTAMP WITH TIME ZONE"))
        logger.info("Added column users.membership_expires_at")


def init_db() -> None:
    """Create all tables if they don't exist. Safe to call multiple times."""
    logger.info("Initializing database tables...")
    Base.metadata.create_all(bind=engine)
    _migrate_options_snapshot_columns()
    _migrate_user_membership_column()
    session = SessionLocal()
    try:
        seed_nav_settings(session)
        seed_discord_menu_author_settings(session)
    finally:
        session.close()
    logger.info("Database tables ready.")
