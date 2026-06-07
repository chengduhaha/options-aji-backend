"""discord author profiles

Revision ID: 20260607_0002
Revises: 20260607_0001
Create Date: 2026-06-07
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260607_0002"
down_revision = "20260607_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "discord_author_profiles" in existing_tables:
        return
    op.create_table(
        "discord_author_profiles",
        sa.Column("author", sa.String(length=256), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=True),
        sa.Column("avatar_filename", sa.String(length=256), nullable=True),
        sa.Column("bio_zh", sa.Text(), nullable=True),
        sa.Column("twitter_handle", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_by_user_id", sa.String(length=36), nullable=True),
        sa.PrimaryKeyConstraint("author"),
    )


def downgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "discord_author_profiles" not in existing_tables:
        return
    op.drop_table("discord_author_profiles")
