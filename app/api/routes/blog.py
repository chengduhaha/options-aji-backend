"""Public blog + admin CRUD for personal IP content."""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Annotated, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.api.deps_auth import get_current_admin_user, get_optional_admin_user
from app.api.deps_membership import get_v3_access
from app.db.models_blog import BlogAttachmentRow, BlogPostRow
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.services.blog_document_sort import sort_documents
from app.services.blog_play_token import create_play_token, decode_play_token
from app.services.blog_storage import BlogStorageError, delete_pdf, resolve_pdf_path, store_pdf
from app.services.blog_thumbnail import (
    BlogThumbnailError,
    delete_thumbnail,
    is_r2_thumbnail_key,
    resolve_thumbnail_path,
    store_thumbnail,
    thumbnail_mime_type,
)
from app.services.membership import V3Access, membership_public_fields
from app.services.r2_storage import (
    R2StorageError,
    fetch_object_range,
    head_object_size,
    presigned_get_url,
    r2_configured,
)

router = APIRouter(tags=["blog"])

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class BlogAttachmentPublic(BaseModel):
    id: str
    original_filename: str
    mime_type: str
    file_size: int
    title_zh: Optional[str] = None
    title_en: Optional[str] = None
    category: str = "general"
    description_zh: Optional[str] = None
    description_en: Optional[str] = None
    is_sample: bool = True
    is_preview: bool = False
    media_kind: str = "document"
    post_id: Optional[str] = None
    download_url: str
    view_url: str
    created_at: Optional[datetime] = None
    #: Video duration in seconds when known (optional until metadata is imported).
    duration_sec: Optional[int] = None
    #: Cover image URL when uploaded (optional).
    thumbnail_url: Optional[str] = None


class BlogPlayTokenResponse(BaseModel):
    token: str
    stream_url: str
    expires_at: datetime
    preview: bool
    preview_seconds: Optional[int] = None


class BlogDocumentListResponse(BaseModel):
    items: list[BlogAttachmentPublic]
    total: int = 0
    page: int = 1
    page_size: int = 20
    categories: list[str] = Field(default_factory=list)
    access: dict[str, object] = Field(default_factory=dict)

class BlogPostSummary(BaseModel):
    id: str
    slug: str
    title_zh: str
    title_en: Optional[str] = None
    excerpt_zh: Optional[str] = None
    excerpt_en: Optional[str] = None
    content_format: str = "markdown"
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


_CONTENT_FORMAT_RE = re.compile(r"^(markdown|html)$")


class BlogPostCreateBody(BaseModel):
    slug: str = Field(min_length=1, max_length=160)
    title_zh: str = Field(min_length=1, max_length=512)
    title_en: Optional[str] = Field(default=None, max_length=512)
    excerpt_zh: Optional[str] = None
    excerpt_en: Optional[str] = None
    body_zh: str = ""
    body_en: Optional[str] = None
    content_format: str = Field(default="markdown", pattern="^(markdown|html)$")
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
    content_format: Optional[str] = Field(default=None, pattern="^(markdown|html)$")
    category: Optional[str] = Field(default=None, max_length=64)
    tags: Optional[list[str]] = None
    status: Optional[str] = Field(default=None, pattern="^(draft|published)$")
    published_at: Optional[datetime] = None


class BlogUploadPdfResponse(BaseModel):
    attachment: BlogAttachmentPublic
    post_id: Optional[str] = None


class BlogAttachmentUpdateBody(BaseModel):
    title_zh: Optional[str] = Field(default=None, max_length=256)
    title_en: Optional[str] = Field(default=None, max_length=256)
    category: Optional[str] = Field(default=None, max_length=64)
    description_zh: Optional[str] = None
    description_en: Optional[str] = None
    is_sample: Optional[bool] = None
    post_id: Optional[str] = None


def _content_disposition(disposition: str, filename: str) -> str:
    safe_ascii = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._") or "document.pdf"
    encoded = quote(filename)
    return f'{disposition}; filename="{safe_ascii}"; filename*=UTF-8\'\'{encoded}'


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
    if attachment.media_kind == "video":
        view = f"/api/blog/attachments/{attachment.id}/stream"
        return view, view
    base = f"/api/blog/attachments/{attachment.id}/file"
    return f"{base}?download=true", base


def _thumbnail_url(attachment: BlogAttachmentRow) -> Optional[str]:
    if not attachment.thumbnail_stored_name:
        return None
    return f"/api/blog/attachments/{attachment.id}/thumbnail"


_GUEST_TEASER_FRACTION = 0.3
_DEFAULT_DOCUMENT_PAGE_SIZE = 20


def _sort_course_rows(rows: list[BlogAttachmentRow], *, sort: str) -> list[BlogAttachmentRow]:
    """Sort standalone course videos by created_at (newest or oldest first)."""
    reverse = sort != "oldest"

    def _key(row: BlogAttachmentRow) -> tuple[datetime, str]:
        created = row.created_at
        if created is None:
            created = datetime.min.replace(tzinfo=timezone.utc)
        elif created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        else:
            created = created.astimezone(timezone.utc)
        return created, row.id

    return sorted(rows, key=_key, reverse=reverse)


def _fetch_standalone_documents(
    session: Session,
    *,
    category: Optional[str] = None,
    media_kind: str = "document",
    sort: str = "newest",
) -> list[BlogAttachmentRow]:
    filters = [
        BlogAttachmentRow.post_id.is_(None),
        BlogAttachmentRow.media_kind == media_kind,
    ]
    if category:
        filters.append(BlogAttachmentRow.category == category.strip())
    rows = session.execute(select(BlogAttachmentRow).where(*filters)).scalars().all()
    if media_kind == "video":
        return _sort_course_rows(list(rows), sort=sort)
    return sort_documents(list(rows))


def _paginate_documents(
    rows: list[BlogAttachmentRow],
    *,
    page: int,
    page_size: int,
) -> tuple[list[BlogAttachmentRow], int]:
    total = len(rows)
    offset = (page - 1) * page_size
    return rows[offset : offset + page_size], total


def _standalone_categories(session: Session, *, media_kind: str = "document") -> list[str]:
    categories = (
        session.execute(
            select(BlogAttachmentRow.category)
            .where(
                BlogAttachmentRow.post_id.is_(None),
                BlogAttachmentRow.media_kind == media_kind,
            )
            .distinct()
            .order_by(BlogAttachmentRow.category)
        )
        .scalars()
        .all()
    )
    return [c for c in categories if c]


def _to_attachment_public(
    attachment: BlogAttachmentRow,
    *,
    is_preview: bool = False,
) -> BlogAttachmentPublic:
    download_url, view_url = _attachment_urls(attachment)
    return BlogAttachmentPublic(
        id=attachment.id,
        original_filename=attachment.original_filename,
        mime_type=attachment.mime_type,
        file_size=attachment.file_size,
        title_zh=attachment.title_zh,
        title_en=attachment.title_en,
        category=attachment.category,
        description_zh=attachment.description_zh,
        description_en=attachment.description_en,
        is_sample=attachment.is_sample,
        is_preview=is_preview,
        media_kind=attachment.media_kind or "document",
        post_id=attachment.post_id,
        download_url=download_url,
        view_url=view_url,
        created_at=attachment.created_at,
        duration_sec=attachment.duration_sec,
        thumbnail_url=_thumbnail_url(attachment),
    )


def _guest_teaser_ids(
    session: Session,
    *,
    category: Optional[str] = None,
    media_kind: str = "document",
) -> set[str]:
    """Newest ceil(30%) of standalone items per category visible to guests."""
    rows = _fetch_standalone_documents(session, category=category, media_kind=media_kind)
    by_category: dict[str, list[BlogAttachmentRow]] = defaultdict(list)
    for row in rows:
        by_category[row.category or "general"].append(row)

    visible: set[str] = set()
    for cat_rows in by_category.values():
        take = max(1, math.ceil(len(cat_rows) * _GUEST_TEASER_FRACTION))
        for row in cat_rows[:take]:
            visible.add(row.id)
    return visible


def _document_category_breakdown(
    session: Session,
    *,
    media_kind: str = "document",
) -> list[dict[str, object]]:
    rows = _fetch_standalone_documents(session, media_kind=media_kind)
    by_category: dict[str, list[BlogAttachmentRow]] = defaultdict(list)
    for row in rows:
        by_category[row.category or "general"].append(row)

    teaser_ids = _guest_teaser_ids(session, media_kind=media_kind)
    breakdown: list[dict[str, object]] = []
    for cat in sorted(by_category.keys()):
        cat_rows = by_category[cat]
        member_count = len(cat_rows)
        guest_visible = sum(1 for row in cat_rows if row.id in teaser_ids)
        breakdown.append(
            {
                "category": cat,
                "member_count": member_count,
                "guest_visible_count": guest_visible,
            }
        )
    return breakdown


def _document_access_fields(
    session: Session,
    access: V3Access,
    *,
    category: Optional[str] = None,
    media_kind: str = "document",
) -> dict[str, object]:
    fields = membership_public_fields(access)
    member_total = len(_fetch_standalone_documents(session, category=category, media_kind=media_kind))
    guest_teaser_count = len(_guest_teaser_ids(session, category=category, media_kind=media_kind))
    visible_count = member_total if access.is_member else guest_teaser_count
    fields.update(
        {
            "visible_count": visible_count,
            "member_total_count": member_total,
            "guest_teaser_count": guest_teaser_count,
            "category_breakdown": (
                _document_category_breakdown(session, media_kind=media_kind) if category is None else []
            ),
        }
    )
    return fields


def _course_thumbnail_is_public(attachment: BlogAttachmentRow) -> bool:
    """Standalone course cover images are marketing assets, not gated like video playback."""
    return attachment.post_id is None and attachment.media_kind == "video"


def _attachment_is_public(
    attachment: BlogAttachmentRow,
    session: Session,
    admin: Optional[UserRow],
    access: Optional[V3Access] = None,
) -> bool:
    if attachment.post_id is None:
        if admin is not None or bool(access and access.is_member):
            return True
        media_kind = attachment.media_kind or "document"
        return attachment.id in _guest_teaser_ids(session, media_kind=media_kind)
    post = session.get(BlogPostRow, attachment.post_id)
    if post is None:
        return admin is not None
    return post.status == "published" or admin is not None


def _video_r2_key(attachment: BlogAttachmentRow) -> str:
    key = (attachment.r2_key or attachment.stored_name or "").strip()
    if not key:
        raise HTTPException(
            status_code=404,
            detail={"code": "file_missing", "message": "视频文件不存在。"},
        )
    return key


def _parse_range_header(range_header: str, total_size: int) -> tuple[int, int]:
    match = re.match(r"bytes=(\d+)-(\d*)", range_header.strip())
    if not match or total_size <= 0:
        raise HTTPException(
            status_code=416,
            detail={"code": "invalid_range", "message": "无效的 Range 请求。"},
        )
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else total_size - 1
    end = min(end, total_size - 1)
    if start > end:
        raise HTTPException(
            status_code=416,
            detail={"code": "invalid_range", "message": "无效的 Range 请求。"},
        )
    return start, end


def _list_standalone_media(
    session: Session,
    access: V3Access,
    *,
    category: Optional[str],
    page: int,
    page_size: int,
    media_kind: str,
    sort: str = "newest",
) -> BlogDocumentListResponse:
    if access.is_member:
        rows = _fetch_standalone_documents(session, category=category, media_kind=media_kind, sort=sort)
        page_rows, total = _paginate_documents(rows, page=page, page_size=page_size)
        items = [_to_attachment_public(r) for r in page_rows]
    else:
        teaser_ids = _guest_teaser_ids(session, category=category, media_kind=media_kind)
        if not teaser_ids:
            rows = []
        else:
            rows = [
                row
                for row in _fetch_standalone_documents(session, category=category, media_kind=media_kind, sort=sort)
                if row.id in teaser_ids
            ]
        page_rows, total = _paginate_documents(rows, page=page, page_size=page_size)
        items = [_to_attachment_public(r, is_preview=True) for r in page_rows]

    return BlogDocumentListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        categories=_standalone_categories(session, media_kind=media_kind),
        access=_document_access_fields(session, access, category=category, media_kind=media_kind),
    )


def _get_standalone_course(
    session: Session,
    access: V3Access,
    attachment_id: str,
) -> BlogAttachmentPublic:
    row = session.get(BlogAttachmentRow, attachment_id)
    if row is None or row.post_id is not None or row.media_kind != "video":
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "视频不存在。"})
    if not _attachment_is_public(row, session, None, access):
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "视频不存在。"})
    is_preview = not access.is_member
    return _to_attachment_public(row, is_preview=is_preview)


def _normalize_content_format(raw: str | None) -> str:
    value = (raw or "markdown").strip().lower()
    if not _CONTENT_FORMAT_RE.match(value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_content_format", "message": "content_format 仅允许 markdown 或 html。"},
        )
    return value


def _to_summary(row: BlogPostRow, attachment_count: int = 0) -> BlogPostSummary:
    return BlogPostSummary(
        id=row.id,
        slug=row.slug,
        title_zh=row.title_zh,
        title_en=row.title_en,
        excerpt_zh=row.excerpt_zh,
        excerpt_en=row.excerpt_en,
        content_format=row.content_format or "markdown",
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
        raise HTTPException(
            status_code=404,
            detail={
                "code": "draft",
                "message": "该文章尚未发布，仅管理员可预览。",
            },
        )

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
    admin: Annotated[Optional[UserRow], Depends(get_optional_admin_user)] = None,
    access: V3Access = Depends(get_v3_access),
) -> FileResponse:
    row = session.get(BlogAttachmentRow, attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "附件不存在。"})
    if row.media_kind == "video":
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "请使用视频播放接口。"},
        )
    if not _attachment_is_public(row, session, admin, access):
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
        headers={"Content-Disposition": _content_disposition(disposition, row.original_filename)},
    )


@router.get("/api/blog/documents", response_model=BlogDocumentListResponse)
def list_blog_documents(
    session: Session = Depends(db_session_dep),
    category: Optional[str] = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(_DEFAULT_DOCUMENT_PAGE_SIZE, ge=1, le=100),
    access: V3Access = Depends(get_v3_access),
) -> BlogDocumentListResponse:
    """Standalone PDF documents: ~30% teaser per category for guests, full archive for members."""
    return _list_standalone_media(
        session,
        access,
        category=category,
        page=page,
        page_size=page_size,
        media_kind="document",
    )


@router.get("/api/blog/courses", response_model=BlogDocumentListResponse)
def list_blog_courses(
    session: Session = Depends(db_session_dep),
    category: Optional[str] = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(_DEFAULT_DOCUMENT_PAGE_SIZE, ge=1, le=100),
    sort: str = Query(default="newest", pattern="^(newest|oldest)$"),
    access: V3Access = Depends(get_v3_access),
) -> BlogDocumentListResponse:
    """Standalone course videos: ~30% teaser per category for guests, full library for members."""
    return _list_standalone_media(
        session,
        access,
        category=category,
        page=page,
        page_size=page_size,
        media_kind="video",
        sort=sort,
    )


@router.get("/api/blog/courses/{attachment_id}", response_model=BlogAttachmentPublic)
def get_blog_course(
    attachment_id: str,
    session: Session = Depends(db_session_dep),
    access: V3Access = Depends(get_v3_access),
) -> BlogAttachmentPublic:
    """Single standalone course video metadata for the watch page."""
    return _get_standalone_course(session, access, attachment_id)


@router.post("/api/blog/attachments/{attachment_id}/play-token", response_model=BlogPlayTokenResponse)
def create_blog_play_token(
    attachment_id: str,
    session: Session = Depends(db_session_dep),
    admin: Annotated[Optional[UserRow], Depends(get_optional_admin_user)] = None,
    access: V3Access = Depends(get_v3_access),
) -> BlogPlayTokenResponse:
    row = session.get(BlogAttachmentRow, attachment_id)
    if row is None or row.media_kind != "video":
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "视频不存在。"})
    if not row.r2_key and not row.stored_name:
        raise HTTPException(status_code=404, detail={"code": "file_missing", "message": "视频文件不存在。"})
    if not r2_configured():
        raise HTTPException(
            status_code=503,
            detail={"code": "storage_unavailable", "message": "视频存储未配置。"},
        )

    is_member = admin is not None or access.is_member
    preview = not is_member
    if preview and not _attachment_is_public(row, session, admin, access):
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "视频不存在。"})

    token, expires_at = create_play_token(attachment_id=row.id, preview=preview)
    stream_url = f"/api/blog/attachments/{row.id}/stream?ticket={quote(token, safe='')}"
    preview_seconds = None
    if preview:
        preview_seconds = max(1, int(get_settings().blog_video_guest_preview_seconds))

    return BlogPlayTokenResponse(
        token=token,
        stream_url=stream_url,
        expires_at=expires_at,
        preview=preview,
        preview_seconds=preview_seconds,
    )


@router.get("/api/blog/attachments/{attachment_id}/stream", response_model=None)
def stream_blog_video(
    attachment_id: str,
    request: Request,
    ticket: str = Query(..., min_length=10),
    session: Session = Depends(db_session_dep),
) -> StreamingResponse | RedirectResponse:
    row = session.get(BlogAttachmentRow, attachment_id)
    if row is None or row.media_kind != "video":
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "视频不存在。"})

    payload = decode_play_token(ticket)
    if payload is None or payload.get("attachment_id") != attachment_id:
        raise HTTPException(status_code=403, detail={"code": "invalid_ticket", "message": "播放凭证无效或已过期。"})

    key = _video_r2_key(row)
    cfg = get_settings()
    if cfg.blog_video_stream_redirect:
        try:
            url = presigned_get_url(
                key,
                expires_in=min(600, max(60, int(cfg.blog_play_token_ttl_seconds))),
                response_content_disposition=_content_disposition("inline", row.original_filename),
            )
        except R2StorageError as exc:
            raise HTTPException(status_code=503, detail={"code": "stream_failed", "message": str(exc)}) from exc
        return RedirectResponse(url=url, status_code=302)

    try:
        total_size = head_object_size(key)
    except R2StorageError as exc:
        raise HTTPException(status_code=404, detail={"code": "file_missing", "message": str(exc)}) from exc

    range_header = request.headers.get("range")
    if range_header:
        start, end = _parse_range_header(range_header, total_size)
    else:
        start, end = 0, total_size - 1

    try:
        fetched = fetch_object_range(key, byte_start=start, byte_end=end)
    except R2StorageError as exc:
        raise HTTPException(status_code=503, detail={"code": "stream_failed", "message": str(exc)}) from exc

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": fetched.content_type or row.mime_type,
        "Content-Disposition": _content_disposition("inline", row.original_filename),
    }
    status_code = status.HTTP_206_PARTIAL_CONTENT if range_header else status.HTTP_200_OK
    if range_header:
        headers["Content-Range"] = f"bytes {fetched.range_start}-{fetched.range_end}/{fetched.total_size}"
        headers["Content-Length"] = str(fetched.content_length)

    def _iter_body() -> object:
        try:
            chunk_size = 1024 * 256
            while True:
                chunk = fetched.body.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            fetched.body.close()

    return StreamingResponse(_iter_body(), status_code=status_code, headers=headers)


@router.get("/api/blog/attachments/{attachment_id}/thumbnail", response_model=None)
def get_blog_attachment_thumbnail(
    attachment_id: str,
    session: Session = Depends(db_session_dep),
    admin: Annotated[Optional[UserRow], Depends(get_optional_admin_user)] = None,
    access: V3Access = Depends(get_v3_access),
) -> FileResponse | StreamingResponse:
    row = session.get(BlogAttachmentRow, attachment_id)
    if row is None or not row.thumbnail_stored_name:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "封面不存在。"})
    if not _course_thumbnail_is_public(row) and not _attachment_is_public(row, session, admin, access):
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "封面不存在。"})

    stored = row.thumbnail_stored_name
    mime = thumbnail_mime_type(stored)
    cache_headers = {"Cache-Control": "public, max-age=3600"}

    if is_r2_thumbnail_key(stored):
        if not r2_configured():
            raise HTTPException(status_code=404, detail={"code": "file_missing", "message": "封面不存在。"})
        try:
            total_size = head_object_size(stored)
            fetched = fetch_object_range(stored, byte_start=0, byte_end=max(0, total_size - 1))
        except R2StorageError as exc:
            raise HTTPException(status_code=404, detail={"code": "file_missing", "message": str(exc)}) from exc

        def _iter_thumb() -> object:
            try:
                while True:
                    chunk = fetched.body.read(1024 * 64)
                    if not chunk:
                        break
                    yield chunk
            finally:
                fetched.body.close()

        headers = {
            **cache_headers,
            "Content-Type": fetched.content_type or mime,
            "Content-Disposition": _content_disposition("inline", f"cover-{attachment_id}.jpg"),
        }
        return StreamingResponse(_iter_thumb(), headers=headers)

    try:
        path = resolve_thumbnail_path(stored)
    except BlogStorageError as exc:
        raise HTTPException(status_code=404, detail={"code": "file_missing", "message": str(exc)}) from exc

    return FileResponse(
        path,
        media_type=mime,
        headers={
            **cache_headers,
            "Content-Disposition": _content_disposition("inline", path.name),
        },
    )


@router.post("/api/blog/attachments/{attachment_id}/thumbnail", response_model=BlogAttachmentPublic)
async def upload_blog_attachment_thumbnail(
    attachment_id: str,
    file: UploadFile = File(...),
    admin: UserRow = Depends(get_current_admin_user),
    session: Session = Depends(db_session_dep),
) -> BlogAttachmentPublic:
    row = session.get(BlogAttachmentRow, attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "附件不存在。"})
    if row.media_kind != "video":
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_media", "message": "仅视频课程支持上传封面。"},
        )

    content = await file.read()
    mime_type = file.content_type or "image/jpeg"
    try:
        stored_name = store_thumbnail(content=content, attachment_id=row.id, mime_type=mime_type)
    except BlogThumbnailError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_thumbnail", "message": str(exc)},
        ) from exc

    if row.thumbnail_stored_name and row.thumbnail_stored_name != stored_name:
        delete_thumbnail(row.thumbnail_stored_name)

    row.thumbnail_stored_name = stored_name
    session.add(row)
    session.commit()
    session.refresh(row)
    return _to_attachment_public(row)


@router.get("/api/blog/attachments", response_model=BlogDocumentListResponse)
def list_blog_attachments_admin(
    admin: UserRow = Depends(get_current_admin_user),
    session: Session = Depends(db_session_dep),
    standalone_only: bool = Query(default=False),
) -> BlogDocumentListResponse:
    stmt = select(BlogAttachmentRow)
    if standalone_only:
        stmt = stmt.where(BlogAttachmentRow.post_id.is_(None))
    rows = sort_documents(list(session.execute(stmt).scalars().all()))
    categories = (
        session.execute(select(BlogAttachmentRow.category).distinct().order_by(BlogAttachmentRow.category))
        .scalars()
        .all()
    )
    return BlogDocumentListResponse(
        items=[_to_attachment_public(r) for r in rows],
        categories=[c for c in categories if c],
    )


@router.put("/api/blog/attachments/{attachment_id}", response_model=BlogAttachmentPublic)
def update_blog_attachment(
    attachment_id: str,
    body: BlogAttachmentUpdateBody,
    admin: UserRow = Depends(get_current_admin_user),
    session: Session = Depends(db_session_dep),
) -> BlogAttachmentPublic:
    row = session.get(BlogAttachmentRow, attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "附件不存在。"})

    if body.title_zh is not None:
        row.title_zh = body.title_zh.strip() or None
    if body.title_en is not None:
        row.title_en = body.title_en.strip() or None
    if body.category is not None:
        row.category = body.category.strip() or "general"
    if body.description_zh is not None:
        row.description_zh = body.description_zh
    if body.description_en is not None:
        row.description_en = body.description_en
    if body.is_sample is not None:
        row.is_sample = body.is_sample
    if body.post_id is not None:
        if body.post_id:
            post = session.get(BlogPostRow, body.post_id)
            if post is None:
                raise HTTPException(status_code=404, detail={"code": "not_found", "message": "文章不存在。"})
        row.post_id = body.post_id or None

    session.add(row)
    session.commit()
    session.refresh(row)
    return _to_attachment_public(row)


@router.delete("/api/blog/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_blog_attachment(
    attachment_id: str,
    admin: UserRow = Depends(get_current_admin_user),
    session: Session = Depends(db_session_dep),
) -> None:
    row = session.get(BlogAttachmentRow, attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "附件不存在。"})
    if row.thumbnail_stored_name:
        delete_thumbnail(row.thumbnail_stored_name)
    if row.media_kind != "video":
        delete_pdf(row.stored_name)
    session.delete(row)
    session.commit()


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
        content_format=_normalize_content_format(body.content_format),
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
    if body.content_format is not None:
        row.content_format = _normalize_content_format(body.content_format)
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
    category: Optional[str] = Form(default=None),
    description_zh: Optional[str] = Form(default=None),
    description_en: Optional[str] = Form(default=None),
    is_sample: Optional[str] = Form(default=None),
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

    sample_flag = True
    if is_sample is not None:
        sample_flag = is_sample.strip().lower() in ("1", "true", "yes", "on")

    attachment = BlogAttachmentRow(
        post_id=post_id,
        stored_name=stored_name,
        original_filename=file.filename or "document.pdf",
        mime_type=file.content_type or "application/pdf",
        file_size=len(content),
        title_zh=title_zh,
        title_en=title_en,
        category=(category or "general").strip() or "general",
        description_zh=description_zh,
        description_en=description_en,
        is_sample=sample_flag,
    )
    session.add(attachment)
    session.commit()
    session.refresh(attachment)
    return BlogUploadPdfResponse(attachment=_to_attachment_public(attachment), post_id=post_id)
