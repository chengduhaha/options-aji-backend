"""Add thumbnail and duration columns to blog_attachments."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260702_0003"
down_revision = "20260702_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "blog_attachments",
        sa.Column("thumbnail_stored_name", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "blog_attachments",
        sa.Column("duration_sec", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("blog_attachments", "duration_sec")
    op.drop_column("blog_attachments", "thumbnail_stored_name")
