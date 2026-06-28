"""Blog API — public list/detail and admin CRUD."""
from __future__ import annotations

import datetime as dt
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps_auth import get_current_admin_user
from app.db.models import Base
from app.db.models_blog import BlogAttachmentRow, BlogPostRow
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.main import create_application


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
    admin = UserRow(
        id="admin-1",
        email="admin@example.com",
        password_hash="x",
        role="admin",
        email_verified=True,
    )
    session.add(admin)
    now = dt.datetime.now(dt.timezone.utc)
    session.add(
        BlogPostRow(
            id="post-1",
            slug="hello-world",
            title_zh="你好世界",
            title_en="Hello World",
            excerpt_zh="摘要",
            body_zh="# 正文",
            category="insights",
            tags="期权,教育",
            status="published",
            published_at=now,
            created_by_user_id=admin.id,
        )
    )
    session.add(
        BlogPostRow(
            id="post-2",
            slug="draft-only",
            title_zh="草稿",
            body_zh="hidden",
            category="general",
            status="draft",
            created_by_user_id=admin.id,
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _admin_client(session: Session) -> TestClient:
    app = create_application()
    admin = session.get(UserRow, "admin-1")
    assert admin is not None

    def _override_db() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[db_session_dep] = _override_db
    app.dependency_overrides[get_current_admin_user] = lambda: admin
    return TestClient(app)


def test_list_published_posts_only(db_session: Session) -> None:
    client = _admin_client(db_session)
    res = client.get("/api/blog/posts")
    assert res.status_code == 200
    payload = res.json()
    assert payload["total"] == 1
    assert payload["items"][0]["slug"] == "hello-world"


def test_get_post_by_slug(db_session: Session) -> None:
    client = _admin_client(db_session)
    res = client.get("/api/blog/posts/hello-world")
    assert res.status_code == 200
    assert res.json()["title_zh"] == "你好世界"
    assert res.json()["tags"] == ["期权", "教育"]


def test_draft_hidden_from_public(db_session: Session) -> None:
    client = _admin_client(db_session)
    res = client.get("/api/blog/posts/draft-only")
    assert res.status_code == 404


def test_admin_create_update_delete_post(db_session: Session) -> None:
    client = _admin_client(db_session)
    create = client.post(
        "/api/blog/posts",
        json={
            "slug": "new-post",
            "title_zh": "新文章",
            "body_zh": "内容",
            "category": "insights",
            "tags": ["test"],
            "status": "published",
        },
    )
    assert create.status_code == 201
    post_id = create.json()["id"]

    update = client.put(
        f"/api/blog/posts/{post_id}",
        json={"title_zh": "更新标题"},
    )
    assert update.status_code == 200
    assert update.json()["title_zh"] == "更新标题"

    delete = client.delete(f"/api/blog/posts/{post_id}")
    assert delete.status_code == 204


def test_upload_and_download_pdf(db_session: Session, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLOG_UPLOAD_DIR", str(tmp_path))
    client = _admin_client(db_session)

    pdf_bytes = b"%PDF-1.4\n% fake pdf for test\n"
    res = client.post(
        "/api/blog/upload-pdf",
        files={"file": ("sample.pdf", pdf_bytes, "application/pdf")},
        data={"post_id": "post-1", "title_zh": "示例报告"},
    )
    assert res.status_code == 200
    attachment_id = res.json()["attachment"]["id"]

    row = db_session.get(BlogAttachmentRow, attachment_id)
    assert row is not None
    assert row.post_id == "post-1"

    download = client.get(f"/api/blog/attachments/{attachment_id}/file")
    assert download.status_code == 200
    assert download.content.startswith(b"%PDF")
