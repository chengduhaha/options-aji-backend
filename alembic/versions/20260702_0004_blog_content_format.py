"""Add content_format column to blog_posts (markdown | html)."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260702_0004"
down_revision = "20260702_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "blog_posts",
        sa.Column(
            "content_format",
            sa.String(length=16),
            nullable=False,
            server_default="markdown",
        ),
    )


def downgrade() -> None:
    op.drop_column("blog_posts", "content_format")
