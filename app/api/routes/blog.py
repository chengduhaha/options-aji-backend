"""Public blog + admin CRUD for personal IP content."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps_auth import get_current_admin_user, get_optional_admin_user
from app.db.models_blog import BlogAttachmentRow, BlogPostRow
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.services.blog_storage import BlogStorageError, delete_pdf, resolve_pdf_path, store_pdf

router = APIRouter(tags=["blog"])

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class BlogAttachmentPublic(BaseModel):
    id: str
    original_filename: str
    mime_type: str
    file_size: int
    title_zh: Optional[str] = None
    title_en: Optional[str] = None
    download_url: str
    view_url: str


class BlogPostSummary(BaseModel):
    id: str
    slug: str
    title_zh: str
    title_en: Optional[str] = None
    excerpt_zh: Optional[str] = None
    excerpt_en: Optional[str] = None
    category: str
    tags: list[str] = Field(default_factory=list)
    status: str
    published_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    attachment_count: int = 0


class BlogPostDetail(BlogPostSummary):
    body_zh: str
    body_en: Optional[str] = None
    attachments: list[BlogAttachmentPublic] = Field(default_factory=list)


class BlogPostListResponse(BaseModel):
    items: list[BlogPostSummary]
    total: int
    page: int
    page_size: int
    categories: list[str] = Field(default_factory=list)


class BlogPostCreateBody(BaseModel):
    slug: str = Field(min_length=1, max_length=160)
    title_zh: str = Field(min_length=1, max_length=512)
    title_en: Optional[str] = Field(default=None, max_length=512)
    excerpt_zh: Optional[str] = None
    excerpt_en: Optional[str] = None
    body_zh: str = ""
    body_en: Optional[str] = None
    category: str = Field(default="general", max_length=64)
    tags: list[str] = Field(default_factory=list)
    status: str = Field(default="draft", pattern="^(draft|published)$")
    published_at: Optional[datetime] = None


class BlogPostUpdateBody(BaseModel):
    slug: Optional[str] = Field(default=None, max_length=160)
    title_zh: Optional[str] = Field(default=None, max_length=512)
    title_en: Optional[str] = Field(default=None, max_length=512)
    excerpt_zh: Optional[str] = None
    excerpt_en: Optional[str] = None
    body_zh: Optional[str] = None
    body_en: Optional[str] = None
    category: Optional[str] = Field(default=None, max_length=64)
    tags: Optional[list[str]] = None
    status: Optional[str] = Field(default=None, pattern="^(draft|published)$")
    published_at: Optional[datetime] = None


class BlogUploadPdfResponse(BaseModel):
    attachment: BlogAttachmentPublic
    post_id: Optional[str] = None


def _parse_tags(raw: str) -> list[str]:
    if not raw.strip():
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _serialize_tags(tags: list[str]) -> str:
    return ",".join(t.strip() for t in tags if t.strip())


def _normalize_slug(slug: str) -> str:
    normalized = slug.strip().lower().replace("_", "-")
    normalized = re.sub(r"[^a-z0-9-]+", "-", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    if not normalized or not _SLUG_RE.match(normalized):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_slug", "message": "slug 仅允许小写字母、数字与连字符。"},
        )
    return normalized


def _attachment_urls(attachment: BlogAttachmentRow) -> tuple[str, str]:
    base = f"/api/blog/attachments/{attachment.id}/file"
    return base, base


def _to_attachment_public(attachment: BlogAttachmentRow) -> BlogAttachmentPublic:
    download_url, view_url = _attachment_urls(attachment)
    return BlogAttachmentPublic(
        id=attachment.id,
        original_filename=attachment.original_filename,
        mime_type=attachment.mime_type,
        file_size=attachment.file_size,
        title_zh=attachment.title_zh,
        title_en=attachment.title_en,
        download_url=download_url,
        view_url=view_url,
    )


def _to_summary(row: BlogPostRow, attachment_count: int = 0) -> BlogPostSummary:
    return BlogPostSummary(
        id=row.id,
        slug=row.slug,
        title_zh=row.title_zh,
        title_en=row.title_en,
        excerpt_zh=row.excerpt_zh,
        excerpt_en=row.excerpt_en,
        category=row.category,
        tags=_parse_tags(row.tags),
        status=row.status,
        published_at=row.published_at,
        updated_at=row.updated_at,
        attachment_count=attachment_count,
    )


def _attachment_count_map(session: Session, post_ids: list[str]) -> dict[str, int]:
    if not post_ids:
        return {}
    rows = session.execute(
        select(BlogAttachmentRow.post_id, func.count())
        .where(BlogAttachmentRow.post_id.in_(post_ids))
        .group_by(BlogAttachmentRow.post_id)
    ).all()
    return {str(post_id): int(count) for post_id, count in rows if post_id}


def _list_categories(session: Session, *, published_only: bool) -> list[str]:
    stmt = select(BlogPostRow.category).distinct().order_by(BlogPostRow.category)
    if published_only:
        stmt = stmt.where(BlogPostRow.status == "published")
    rows = session.execute(stmt).scalars().all()
    return [c for c in rows if c]


@router.get("/api/blog/posts", response_model=BlogPostListResponse)
def list_blog_posts(
    session: Session = Depends(db_session_dep),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    category: Optional[str] = Query(default=None),
    include_drafts: bool = Query(default=False),
    admin: Annotated[Optional[UserRow], Depends(get_optional_admin_user)] = None,
) -> BlogPostListResponse:
    show_drafts = include_drafts and admin is not None
    filters = []
    if not show_drafts:
        filters.append(BlogPostRow.status == "published")

    if category:
        filters.append(BlogPostRow.category == category.strip())

    count_stmt = select(func.count()).select_from(BlogPostRow)
    list_stmt = select(BlogPostRow)
    if filters:
        count_stmt = count_stmt.where(*filters)
        list_stmt = list_stmt.where(*filters)

    total = int(session.execute(count_stmt).scalar_one())
    offset = (page - 1) * page_size
    rows = (
        session.execute(
            list_stmt.order_by(
                BlogPostRow.published_at.desc().nullslast(),
                BlogPostRow.updated_at.desc(),
            )
            .offset(offset)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    counts = _attachment_count_map(session, [r.id for r in rows])
    return BlogPostListResponse(
        items=[_to_summary(r, counts.get(r.id, 0)) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        categories=_list_categories(session, published_only=not show_drafts),
    )


@router.get("/api/blog/posts/{slug}", response_model=BlogPostDetail)
def get_blog_post_by_slug(
    slug: str,
    session: Session = Depends(db_session_dep),
    admin: Annotated[Optional[UserRow], Depends(get_optional_admin_user)] = None,
) -> BlogPostDetail:
    row = session.execute(select(BlogPostRow).where(BlogPostRow.slug == slug)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "文章不存在。"})
    if row.status != "published" and admin is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "文章不存在。"})

    attachments = (
        session.execute(select(BlogAttachmentRow).where(BlogAttachmentRow.post_id == row.id))
        .scalars()
        .all()
    )
    summary = _to_summary(row, len(attachments))
    return BlogPostDetail(
        **summary.model_dump(),
        body_zh=row.body_zh,
        body_en=row.body_en,
        attachments=[_to_attachment_public(a) for a in attachments],
    )


@router.get("/api/blog/attachments/{attachment_id}/file")
def download_blog_attachment(
    attachment_id: str,
    session: Session = Depends(db_session_dep),
    download: bool = Query(default=False),
) -> FileResponse:
    row = session.get(BlogAttachmentRow, attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "附件不存在。"})
    if row.post_id:
        post = session.get(BlogPostRow, row.post_id)
        if post is None or post.status != "published":
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "附件不存在。"})

    try:
        path = resolve_pdf_path(row.stored_name)
    except BlogStorageError as exc:
        raise HTTPException(status_code=404, detail={"code": "file_missing", "message": str(exc)}) from exc

    disposition = "attachment" if download else "inline"
    return FileResponse(
        path,
        media_type=row.mime_type,
        filename=row.original_filename,
        headers={"Content-Disposition": f'{disposition}; filename="{row.original_filename}"'},
    )


@router.post("/api/blog/posts", response_model=BlogPostDetail, status_code=status.HTTP_201_CREATED)
def create_blog_post(
    body: BlogPostCreateBody,
    admin: UserRow = Depends(get_current_admin_user),
    session: Session = Depends(db_session_dep),
) -> BlogPostDetail:
    slug = _normalize_slug(body.slug)
    exists = session.execute(select(BlogPostRow.id).where(BlogPostRow.slug == slug)).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=409, detail={"code": "slug_taken", "message": "slug 已存在。"})

    now = datetime.now(timezone.utc)
    published_at = body.published_at
    if body.status == "published" and published_at is None:
        published_at = now

    row = BlogPostRow(
        slug=slug,
        title_zh=body.title_zh.strip(),
        title_en=body.title_en.strip() if body.title_en else None,
        excerpt_zh=body.excerpt_zh,
        excerpt_en=body.excerpt_en,
        body_zh=body.body_zh,
        body_en=body.body_en,
        category=body.category.strip() or "general",
        tags=_serialize_tags(body.tags),
        status=body.status,
        published_at=published_at,
        created_by_user_id=admin.id,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return BlogPostDetail(**_to_summary(row).model_dump(), body_zh=row.body_zh, body_en=row.body_en, attachments=[])


@router.put("/api/blog/posts/{post_id}", response_model=BlogPostDetail)
def update_blog_post(
    post_id: str,
    body: BlogPostUpdateBody,
    admin: UserRow = Depends(get_current_admin_user),
    session: Session = Depends(db_session_dep),
) -> BlogPostDetail:
    row = session.get(BlogPostRow, post_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "文章不存在。"})

    if body.slug is not None:
        slug = _normalize_slug(body.slug)
        conflict = session.execute(
            select(BlogPostRow.id).where(BlogPostRow.slug == slug, BlogPostRow.id != post_id)
        ).scalar_one_or_none()
        if conflict:
            raise HTTPException(status_code=409, detail={"code": "slug_taken", "message": "slug 已存在。"})
        row.slug = slug

    if body.title_zh is not None:
        row.title_zh = body.title_zh.strip()
    if body.title_en is not None:
        row.title_en = body.title_en.strip() or None
    if body.excerpt_zh is not None:
        row.excerpt_zh = body.excerpt_zh
    if body.excerpt_en is not None:
        row.excerpt_en = body.excerpt_en
    if body.body_zh is not None:
        row.body_zh = body.body_zh
    if body.body_en is not None:
        row.body_en = body.body_en or None
    if body.category is not None:
        row.category = body.category.strip() or "general"
    if body.tags is not None:
        row.tags = _serialize_tags(body.tags)
    if body.status is not None:
        row.status = body.status
        if body.status == "published" and row.published_at is None:
            row.published_at = datetime.now(timezone.utc)
    if body.published_at is not None:
        row.published_at = body.published_at

    session.add(row)
    session.commit()
    session.refresh(row)

    attachments = (
        session.execute(select(BlogAttachmentRow).where(BlogAttachmentRow.post_id == row.id))
        .scalars()
        .all()
    )
    return BlogPostDetail(
        **_to_summary(row, len(attachments)).model_dump(),
        body_zh=row.body_zh,
        body_en=row.body_en,
        attachments=[_to_attachment_public(a) for a in attachments],
    )


@router.delete("/api/blog/posts/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_blog_post(
    post_id: str,
    admin: UserRow = Depends(get_current_admin_user),
    session: Session = Depends(db_session_dep),
) -> None:
    row = session.get(BlogPostRow, post_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "文章不存在。"})

    attachments = (
        session.execute(select(BlogAttachmentRow).where(BlogAttachmentRow.post_id == row.id))
        .scalars()
        .all()
    )
    for attachment in attachments:
        delete_pdf(attachment.stored_name)
        session.delete(attachment)
    session.delete(row)
    session.commit()


@router.post("/api/blog/upload-pdf", response_model=BlogUploadPdfResponse)
async def upload_blog_pdf(
    file: UploadFile = File(...),
    post_id: Optional[str] = Form(default=None),
    title_zh: Optional[str] = Form(default=None),
    title_en: Optional[str] = Form(default=None),
    admin: UserRow = Depends(get_current_admin_user),
    session: Session = Depends(db_session_dep),
) -> BlogUploadPdfResponse:
    if post_id:
        post = session.get(BlogPostRow, post_id)
        if post is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "文章不存在。"})

    content = await file.read()
    try:
        stored_name, _ = store_pdf(content=content, original_filename=file.filename or "document.pdf")
    except BlogStorageError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_pdf", "message": str(exc)}) from exc

    attachment = BlogAttachmentRow(
        post_id=post_id,
        stored_name=stored_name,
        original_filename=file.filename or "document.pdf",
        mime_type=file.content_type or "application/pdf",
        file_size=len(content),
        title_zh=title_zh,
        title_en=title_en,
    )
    session.add(attachment)
    session.commit()
    session.refresh(attachment)
    return BlogUploadPdfResponse(attachment=_to_attachment_public(attachment), post_id=post_id)
