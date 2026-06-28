"""Add category and description fields to blog_attachments."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260628_0002"
down_revision = "20260628_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "blog_attachments",
        sa.Column("category", sa.String(length=64), nullable=False, server_default="general"),
    )
    op.add_column("blog_attachments", sa.Column("description_zh", sa.Text(), nullable=True))
    op.add_column("blog_attachments", sa.Column("description_en", sa.Text(), nullable=True))
    op.add_column(
        "blog_attachments",
        sa.Column("is_sample", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_index("idx_blog_attachments_category", "blog_attachments", ["category"])


def downgrade() -> None:
    op.drop_index("idx_blog_attachments_category", table_name="blog_attachments")
    op.drop_column("blog_attachments", "is_sample")
    op.drop_column("blog_attachments", "description_en")
    op.drop_column("blog_attachments", "description_zh")
    op.drop_column("blog_attachments", "category")
