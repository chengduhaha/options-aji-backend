"""Database engine / session factories."""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def _engine():
    settings = get_settings()
    connect_args = {}
    url = settings.database_url
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


engine = _engine()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def db_session_dep() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
