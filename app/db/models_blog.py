"""Blog posts and PDF attachments for personal IP content."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


class BlogPostRow(Base):
    __tablename__ = "blog_posts"
    __table_args__ = (
        Index("idx_blog_posts_slug", "slug", unique=True),
        Index("idx_blog_posts_status_published", "status", "published_at"),
        Index("idx_blog_posts_category", "category"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    slug: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    title_zh: Mapped[str] = mapped_column(String(512), nullable=False)
    title_en: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    excerpt_zh: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    excerpt_en: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    body_zh: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_en: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: ``markdown`` (default) or ``html`` — HTML is stored in body_zh/body_en.
    content_format: Mapped[str] = mapped_column(String(16), nullable=False, default="markdown")
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    members_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=True,
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=True,
    )


class BlogAttachmentRow(Base):
    __tablename__ = "blog_attachments"
    __table_args__ = (
        Index("idx_blog_attachments_post_id", "post_id"),
        Index("idx_blog_attachments_stored_name", "stored_name", unique=True),
        Index("idx_blog_attachments_category", "category"),
        Index("idx_blog_attachments_baidu_fs_id", "baidu_fs_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    post_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("blog_posts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    stored_name: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(256), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False, default="application/pdf")
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title_zh: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    title_en: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    description_zh: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    description_en: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_sample: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: ``document`` (local PDF) or ``video`` (R2 object).
    media_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="document")
    #: R2 object key when media_kind is video.
    r2_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    #: Baidu Netdisk fs_id for import deduplication.
    baidu_fs_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: Local filename or R2 key for course cover image.
    thumbnail_stored_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    #: Video duration in seconds when known.
    duration_sec: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=True,
    )
