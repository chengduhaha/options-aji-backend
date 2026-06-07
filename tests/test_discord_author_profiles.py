"""Discord author profiles — avatars, kol-hub, timeline pagination."""
from __future__ import annotations

import datetime as dt
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps_auth import get_current_admin_user
from app.db.models import Base, DiscordMessageRow
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.main import create_application
from app.services.discord_author_profiles import avatar_storage_dir, save_avatar_file
from app.services.cache_service import cache_delete
from app.services.discord_menu_authors import CACHE_KEY_SETTINGS, save_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Generator[None, None, None]:
    cache_delete(CACHE_KEY_SETTINGS)
    yield
    cache_delete(CACHE_KEY_SETTINGS)


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
    now = dt.datetime.now(dt.timezone.utc)
    authors = ("Alpha • TweetShift", "Beta • TweetShift", "Gamma • TweetShift")
    for idx, author in enumerate(authors, start=1):
        session.add(
            DiscordMessageRow(
                id=str(idx),
                channel_id="ch1",
                author=author,
                content=f"msg {idx}",
                timestamp=now - dt.timedelta(hours=idx),
                tickers=["SPY"],
            )
        )
    session.commit()
    try:
        yield session
    finally:
        session.close()


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
    return TestClient(app)


def _png_bytes() -> bytes:
    # minimal valid 1x1 PNG
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def test_avatar_upload_and_serve(db_session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr("app.services.discord_author_profiles.avatar_storage_dir", lambda: tmp_path)
    content = _png_bytes()
    row = save_avatar_file(
        db_session,
        author="Alpha • TweetShift",
        content=content,
        content_type="image/png",
    )
    assert row.avatar_filename
    client = _admin_client(db_session)
    resp = client.get(f"/api/discord/avatars/{row.avatar_filename}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/")


def test_kol_hub_respects_whitelist(db_session: Session) -> None:
    cache_delete(CACHE_KEY_SETTINGS)
    save_settings(db_session, {"twitter_kol": ["Beta • TweetShift"]})
    client = _admin_client(db_session)
    resp = client.get("/api/discord/kol-hub?menu_slot=twitter_kol")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["author"] == "Beta • TweetShift"


def test_timeline_authors_and_pagination(db_session: Session) -> None:
    client = _admin_client(db_session)
    first = client.get(
        "/api/discord/timeline?menu_slot=twitter_kol&limit=1&hours=168"
    )
    assert first.status_code == 200
    body = first.json()
    assert len(body["items"]) == 1
    assert body["has_more"] is True
    assert body["next_before"]

    second = client.get(
        "/api/discord/timeline?menu_slot=twitter_kol&limit=1&hours=168"
        f"&before_timestamp={body['next_before']}"
    )
    assert second.status_code == 200
    assert len(second.json()["items"]) == 1
    assert first.json()["items"][0]["id"] != second.json()["items"][0]["id"]


def test_timeline_authors_filter(db_session: Session) -> None:
    client = _admin_client(db_session)
    resp = client.get(
        "/api/discord/timeline",
        params={
            "menu_slot": "twitter_kol",
            "authors": "Alpha • TweetShift",
            "limit": 10,
        },
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["author"] == "Alpha • TweetShift"
