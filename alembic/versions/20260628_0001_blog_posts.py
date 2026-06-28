"""Alembic migration: blog_posts and blog_attachments tables."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260628_0001"
down_revision = "20260614_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "blog_posts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("title_zh", sa.String(length=512), nullable=False),
        sa.Column("title_en", sa.String(length=512), nullable=True),
        sa.Column("excerpt_zh", sa.Text(), nullable=True),
        sa.Column("excerpt_en", sa.Text(), nullable=True),
        sa.Column("body_zh", sa.Text(), nullable=False, server_default=""),
        sa.Column("body_en", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=False, server_default="general"),
        sa.Column("tags", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("idx_blog_posts_status_published", "blog_posts", ["status", "published_at"])
    op.create_index("idx_blog_posts_category", "blog_posts", ["category"])

    op.create_table(
        "blog_attachments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("post_id", sa.String(length=36), nullable=True),
        sa.Column("stored_name", sa.String(length=256), nullable=False),
        sa.Column("original_filename", sa.String(length=256), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False, server_default="application/pdf"),
        sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("title_zh", sa.String(length=256), nullable=True),
        sa.Column("title_en", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["post_id"], ["blog_posts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stored_name"),
    )
    op.create_index("idx_blog_attachments_post_id", "blog_attachments", ["post_id"])


def downgrade() -> None:
    op.drop_index("idx_blog_attachments_post_id", table_name="blog_attachments")
    op.drop_table("blog_attachments")
    op.drop_index("idx_blog_posts_category", table_name="blog_posts")
    op.drop_index("idx_blog_posts_status_published", table_name="blog_posts")
    op.drop_table("blog_posts")
