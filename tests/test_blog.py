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
from app.api.deps_membership import get_v3_access
from app.db.models import Base
from app.db.models_blog import BlogAttachmentRow, BlogPostRow
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.main import create_application
from app.services.membership import V3Access


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


def _client_with_access(session: Session, access: V3Access) -> TestClient:
    app = create_application()

    def _override_db() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[db_session_dep] = _override_db
    app.dependency_overrides[get_v3_access] = lambda: access
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
    assert res.json()["error"]["code"] == "draft"


def test_admin_can_view_draft_with_auth(db_session: Session) -> None:
    from app.api.deps_auth import get_optional_admin_user

    app = create_application()
    admin = db_session.get(UserRow, "admin-1")
    assert admin is not None

    def _override_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[db_session_dep] = _override_db
    app.dependency_overrides[get_optional_admin_user] = lambda: admin
    client = TestClient(app)

    res = client.get("/api/blog/posts/draft-only")
    assert res.status_code == 200
    assert res.json()["slug"] == "draft-only"
    assert res.json()["status"] == "draft"


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
        files={"file": ("示例报告.pdf", pdf_bytes, "application/pdf")},
        data={
            "post_id": "post-1",
            "title_zh": "示例报告",
            "category": "market-report",
            "description_zh": "每日深度分析",
            "is_sample": "true",
        },
    )
    assert res.status_code == 200
    attachment_id = res.json()["attachment"]["id"]
    assert res.json()["attachment"]["category"] == "market-report"

    row = db_session.get(BlogAttachmentRow, attachment_id)
    assert row is not None
    assert row.post_id == "post-1"

    download = client.get(f"/api/blog/attachments/{attachment_id}/file")
    assert download.status_code == 200
    assert download.content.startswith(b"%PDF")
    assert "filename*=" in download.headers.get("content-disposition", "")


def test_standalone_sample_documents(db_session: Session, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLOG_UPLOAD_DIR", str(tmp_path))
    client = _admin_client(db_session)

    pdf_bytes = b"%PDF-1.4\n% standalone sample\n"
    upload = client.post(
        "/api/blog/upload-pdf",
        files={"file": ("sample.pdf", pdf_bytes, "application/pdf")},
        data={
            "title_zh": "示例异动报告",
            "category": "unusual",
            "description_zh": "每日推送",
            "is_sample": "true",
        },
    )
    assert upload.status_code == 200
    attachment_id = upload.json()["attachment"]["id"]

    public = client.get("/api/blog/documents")
    assert public.status_code == 200
    payload = public.json()
    assert payload["total"] if "total" in payload else len(payload["items"]) >= 1
    assert any(item["id"] == attachment_id for item in payload["items"])

    delete = client.delete(f"/api/blog/attachments/{attachment_id}")
    assert delete.status_code == 204


def test_member_documents_include_non_sample_standalone_pdfs(db_session: Session, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLOG_UPLOAD_DIR", str(tmp_path))
    stored_name, _ = __import__("app.services.blog_storage", fromlist=["store_pdf"]).store_pdf(
        content=b"%PDF-1.4\n% member pdf\n",
        original_filename="member-report.pdf",
    )
    db_session.add(
        BlogAttachmentRow(
            id="member-doc-1",
            stored_name=stored_name,
            original_filename="member-report.pdf",
            mime_type="application/pdf",
            file_size=24,
            title_zh="会员报告",
            category="market-report",
            description_zh="仅会员可见",
            is_sample=False,
        )
    )
    db_session.commit()

    guest = _client_with_access(
        db_session,
        V3Access(tier="guest", is_member=False, membership_expires_at=None, days_remaining=None),
    )
    guest_res = guest.get("/api/blog/documents")
    assert guest_res.status_code == 200
    guest_items = guest_res.json()["items"]
    assert len(guest_items) == 1
    assert guest_items[0]["id"] == "member-doc-1"
    assert guest_items[0]["is_preview"] is True

    member = _client_with_access(
        db_session,
        V3Access(tier="member", is_member=True, membership_expires_at=None, days_remaining=None),
    )
    member_res = member.get("/api/blog/documents")
    assert member_res.status_code == 200
    payload = member_res.json()
    assert payload["access"]["is_member"] is True
    assert any(item["id"] == "member-doc-1" for item in payload["items"])
    assert "market-report" in payload["categories"]


def test_guest_teaser_thirty_percent_per_category(db_session: Session, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLOG_UPLOAD_DIR", str(tmp_path))
    now = dt.datetime.now(dt.timezone.utc)
    store_pdf = __import__("app.services.blog_storage", fromlist=["store_pdf"]).store_pdf

    for index in range(10):
        stored_name, _ = store_pdf(
            content=f"%PDF-1.4\n% course {index}\n".encode(),
            original_filename=f"course-{index:02d}.pdf",
        )
        db_session.add(
            BlogAttachmentRow(
                id=f"course-doc-{index}",
                stored_name=stored_name,
                original_filename=f"course-{index:02d}.pdf",
                mime_type="application/pdf",
                file_size=32,
                title_zh=f"课程 {index}",
                category="course",
                is_sample=False,
                created_at=now - dt.timedelta(days=index),
            )
        )

    for index in range(4):
        stored_name, _ = store_pdf(
            content=f"%PDF-1.4\n% unusual {index}\n".encode(),
            original_filename=f"unusual-{index}.pdf",
        )
        db_session.add(
            BlogAttachmentRow(
                id=f"unusual-doc-{index}",
                stored_name=stored_name,
                original_filename=f"unusual-{index}.pdf",
                mime_type="application/pdf",
                file_size=32,
                title_zh=f"异动 {index}",
                category="unusual-flow",
                is_sample=False,
                created_at=now - dt.timedelta(hours=index),
            )
        )
    db_session.commit()

    guest = _client_with_access(
        db_session,
        V3Access(tier="guest", is_member=False, membership_expires_at=None, days_remaining=None),
    )
    res = guest.get("/api/blog/documents")
    assert res.status_code == 200
    payload = res.json()
    visible_ids = {item["id"] for item in payload["items"]}
    assert len([item for item in payload["items"] if item["category"] == "course"]) == 3
    assert len([item for item in payload["items"] if item["category"] == "unusual-flow"]) == 2
    assert all(item["is_preview"] for item in payload["items"])
    assert "course" in payload["categories"]
    assert "unusual-flow" in payload["categories"]

    newest_course_ids = {f"course-doc-{index}" for index in range(3)}
    assert newest_course_ids.issubset(visible_ids)

    locked = guest.get("/api/blog/attachments/course-doc-9/file")
    assert locked.status_code == 404
    preview = guest.get("/api/blog/attachments/course-doc-0/file")
    assert preview.status_code == 200


def test_member_only_pdf_download_requires_member_access(db_session: Session, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLOG_UPLOAD_DIR", str(tmp_path))
    now = dt.datetime.now(dt.timezone.utc)
    store_pdf = __import__("app.services.blog_storage", fromlist=["store_pdf"]).store_pdf
    stored_name, _ = store_pdf(
        content=b"%PDF-1.4\n% locked pdf\n",
        original_filename="locked-report.pdf",
    )
    db_session.add(
        BlogAttachmentRow(
            id="locked-doc-1",
            stored_name=stored_name,
            original_filename="locked-report.pdf",
            mime_type="application/pdf",
            file_size=24,
            title_zh="锁定报告",
            category="market-report",
            is_sample=False,
            created_at=now,
        )
    )
    for index in range(4):
        extra_name, _ = store_pdf(
            content=f"%PDF-1.4\n% extra {index}\n".encode(),
            original_filename=f"extra-{index}.pdf",
        )
        db_session.add(
            BlogAttachmentRow(
                id=f"extra-doc-{index}",
                stored_name=extra_name,
                original_filename=f"extra-{index}.pdf",
                mime_type="application/pdf",
                file_size=24,
                title_zh=f"额外 {index}",
                category="market-report",
                is_sample=False,
                created_at=now - dt.timedelta(days=index + 1),
            )
        )
    db_session.commit()

    guest = _client_with_access(
        db_session,
        V3Access(tier="guest", is_member=False, membership_expires_at=None, days_remaining=None),
    )
    assert guest.get("/api/blog/attachments/locked-doc-1/file").status_code == 200
    assert guest.get("/api/blog/attachments/extra-doc-3/file").status_code == 404

    member = _client_with_access(
        db_session,
        V3Access(tier="member", is_member=True, membership_expires_at=None, days_remaining=None),
    )
    download = member.get("/api/blog/attachments/locked-doc-1/file")
    assert download.status_code == 200
    assert download.content.startswith(b"%PDF")


def test_member_documents_pagination(db_session: Session, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLOG_UPLOAD_DIR", str(tmp_path))
    now = dt.datetime.now(dt.timezone.utc)
    store_pdf = __import__("app.services.blog_storage", fromlist=["store_pdf"]).store_pdf

    for index in range(25):
        stored_name, _ = store_pdf(
            content=f"%PDF-1.4\n% doc {index}\n".encode(),
            original_filename=f"report_202601{index + 1:02d}.pdf",
        )
        db_session.add(
            BlogAttachmentRow(
                id=f"page-doc-{index}",
                stored_name=stored_name,
                original_filename=f"report_202601{index + 1:02d}.pdf",
                mime_type="application/pdf",
                file_size=32,
                title_zh=f"报告 {index}",
                category="market-report",
                is_sample=False,
                created_at=now - dt.timedelta(days=index),
            )
        )
    db_session.commit()

    member = _client_with_access(
        db_session,
        V3Access(tier="member", is_member=True, membership_expires_at=None, days_remaining=None),
    )
    page1 = member.get("/api/blog/documents?page=1&page_size=20")
    assert page1.status_code == 200
    payload1 = page1.json()
    assert payload1["total"] == 25
    assert payload1["page"] == 1
    assert payload1["page_size"] == 20
    assert len(payload1["items"]) == 20
    assert payload1["items"][0]["id"] == "page-doc-24"

    page2 = member.get("/api/blog/documents?page=2&page_size=20")
    payload2 = page2.json()
    assert len(payload2["items"]) == 5
    assert payload2["items"][0]["id"] == "page-doc-4"


def test_guest_teaser_uses_filename_date_for_newest(db_session: Session, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLOG_UPLOAD_DIR", str(tmp_path))
    now = dt.datetime.now(dt.timezone.utc)
    store_pdf = __import__("app.services.blog_storage", fromlist=["store_pdf"]).store_pdf

    docs = [
        ("course-old-date", "course_20260101.pdf", now),
        ("course-new-date", "course_20260601.pdf", now - dt.timedelta(days=365)),
        ("course-no-date", "course-notes.pdf", now),
    ]
    for doc_id, filename, created_at in docs:
        stored_name, _ = store_pdf(content=b"%PDF-1.4\n", original_filename=filename)
        db_session.add(
            BlogAttachmentRow(
                id=doc_id,
                stored_name=stored_name,
                original_filename=filename,
                mime_type="application/pdf",
                file_size=32,
                title_zh=doc_id,
                category="course",
                is_sample=False,
                created_at=created_at,
            )
        )
    db_session.commit()

    guest = _client_with_access(
        db_session,
        V3Access(tier="guest", is_member=False, membership_expires_at=None, days_remaining=None),
    )
    res = guest.get("/api/blog/documents?category=course")
    assert res.status_code == 200
    visible_ids = [item["id"] for item in res.json()["items"]]
    assert visible_ids == ["course-new-date"]

