"""discord messages author timestamp index

Revision ID: 20260608_0001
Revises: 20260607_0003
Create Date: 2026-06-08
"""
from __future__ import annotations

from alembic import op

revision = "20260608_0001"
down_revision = "20260607_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_discord_messages_author_timestamp",
        "discord_messages",
        ["author", "timestamp"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("idx_discord_messages_author_timestamp", table_name="discord_messages")
