"""Alembic migration: congress_member_profiles table."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260614_0001"
down_revision = "20260613_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "congress_member_profiles",
        sa.Column("member_name", sa.String(length=256), nullable=False),
        sa.Column("chamber", sa.String(length=16), nullable=False),
        sa.Column("bio_zh", sa.Text(), nullable=True),
        sa.Column("party", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=True),
        sa.Column("committee", sa.String(length=256), nullable=True),
        sa.Column("notable_trades_summary", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("member_name", "chamber"),
    )


def downgrade() -> None:
    op.drop_table("congress_member_profiles")
