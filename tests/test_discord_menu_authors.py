"""Discord menu author whitelist — filter, settings, admin API."""
from __future__ import annotations

import datetime as dt
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps_auth import get_current_admin_user, get_current_user
from app.db.models import Base, DiscordMenuAuthorSettingsRow, DiscordMessageRow
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.ingest.message_store import list_discord_feed_rows, list_messages_recent
from app.main import create_application
from app.services.discord_menu_authors import (
    resolve_author_filter,
    save_settings,
    seed_discord_menu_author_settings,
)


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = factory()
    seed_discord_menu_author_settings(session)
    now = dt.datetime.now(dt.timezone.utc)
    session.add(
        DiscordMessageRow(
            id="1",
            channel_id="ch1",
            author="Alpha • TweetShift",
            content="SPY up",
            timestamp=now,
            tickers=["SPY"],
        )
    )
    session.add(
        DiscordMessageRow(
            id="2",
            channel_id="ch1",
            author="Beta • TweetShift",
            content="QQQ down",
            timestamp=now - dt.timedelta(hours=1),
            tickers=["QQQ"],
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()


def test_empty_whitelist_means_no_filter(db_session: Session) -> None:
    assert resolve_author_filter(db_session, "aji_insights") is None
    rows = list_discord_feed_rows(db_session, ticker=None, hours=24, limit=10)
    assert len(rows) == 2


def test_author_filter_restricts_rows(db_session: Session) -> None:
    save_settings(
        db_session,
        {"aji_insights": ["Alpha • TweetShift"]},
    )
    authors = resolve_author_filter(db_session, "aji_insights")
    assert authors == ["Alpha • TweetShift"]
    rows = list_discord_feed_rows(
        db_session,
        ticker=None,
        hours=24,
        limit=10,
        authors=authors,
    )
    assert len(rows) == 1
    assert rows[0].author == "Alpha • TweetShift"


def test_list_messages_recent_author_filter(db_session: Session) -> None:
    save_settings(db_session, {"messages": ["Beta • TweetShift"]})
    authors = resolve_author_filter(db_session, "messages")
    rows = list_messages_recent(
        db_session,
        ticker=None,
        hours=24,
        limit=10,
        authors=authors,
    )
    assert len(rows) == 1
    assert rows[0].author == "Beta • TweetShift"


def _admin_client(session: Session) -> TestClient:
    app = create_application()
    admin = UserRow(
        id="admin-1",
        email="admin@example.com",
        password_hash="x",
        role="admin",
        email_verified=True,
    )

    def _override_db() -> Generator[Session, None, None]:
        yield session

    async def _override_admin() -> UserRow:
        return admin

    app.dependency_overrides[db_session_dep] = _override_db
    app.dependency_overrides[get_current_admin_user] = _override_admin
    app.dependency_overrides[get_current_user] = _override_admin
    return TestClient(app)


def test_admin_discord_authors_endpoint(db_session: Session) -> None:
    client = _admin_client(db_session)
    resp = client.get("/api/admin/discord/authors")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["authors"]) == 2
    names = {row["author"] for row in body["authors"]}
    assert "Alpha • TweetShift" in names
    assert "Beta • TweetShift" in names


def test_put_discord_menu_authors(db_session: Session) -> None:
    client = _admin_client(db_session)
    resp = client.put(
        "/api/admin/discord-menu-authors",
        json={"settings": {"twitter_kol": ["Beta • TweetShift"]}},
    )
    assert resp.status_code == 200
    settings = resp.json()["settings"]
    assert settings["twitter_kol"] == ["Beta • TweetShift"]
    row = db_session.get(DiscordMenuAuthorSettingsRow, "twitter_kol")
    assert row is not None
    assert row.allowed_authors == ["Beta • TweetShift"]


def test_discord_timeline_respects_menu_slot(db_session: Session) -> None:
    save_settings(db_session, {"twitter_kol": ["Alpha • TweetShift"]})
    client = _admin_client(db_session)
    resp = client.get("/api/discord/timeline?menu_slot=twitter_kol&hours=24&limit=10")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["author"] == "Alpha • TweetShift"
