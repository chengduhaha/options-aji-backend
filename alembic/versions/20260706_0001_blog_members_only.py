"""Add members_only column to blog_posts."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260706_0001"
down_revision = "20260702_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "blog_posts",
        sa.Column(
            "members_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("blog_posts", "members_only")
