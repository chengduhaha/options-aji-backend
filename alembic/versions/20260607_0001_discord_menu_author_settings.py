"""discord menu author settings

Revision ID: 20260607_0001
Revises: 20260606_0001
Create Date: 2026-06-07
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260607_0001"
down_revision = "20260606_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "discord_menu_author_settings" in existing_tables:
        return
    op.create_table(
        "discord_menu_author_settings",
        sa.Column("menu_slot", sa.String(length=64), nullable=False),
        sa.Column("allowed_authors", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_by_user_id", sa.String(length=36), nullable=True),
        sa.PrimaryKeyConstraint("menu_slot"),
    )


def downgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "discord_menu_author_settings" not in existing_tables:
        return
    op.drop_table("discord_menu_author_settings")
