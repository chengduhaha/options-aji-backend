"""Database table initialization."""
from __future__ import annotations

import logging

from app.db.models import Base
from app.db.session import engine

logger = logging.getLogger(__name__)


def init_db() -> None:
    """Create all tables if they don't exist. Safe to call multiple times."""
    logger.info("Initializing database tables...")
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables ready.")
