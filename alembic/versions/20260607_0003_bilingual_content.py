"""bilingual content columns

Revision ID: 20260607_0003
Revises: 20260607_0002
Create Date: 2026-06-07
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260607_0003"
down_revision = "20260607_0002"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    if _has_column("message_enrichment", "title_zh"):
        if not _has_column("message_enrichment", "title_en"):
            op.add_column("message_enrichment", sa.Column("title_en", sa.String(length=512), nullable=True))
        if not _has_column("message_enrichment", "summary_en"):
            op.add_column("message_enrichment", sa.Column("summary_en", sa.Text(), nullable=True))
        if not _has_column("message_enrichment", "bullets_en"):
            op.add_column(
                "message_enrichment",
                sa.Column("bullets_en", sa.JSON(), nullable=False, server_default="[]"),
            )
        if not _has_column("message_enrichment", "risk_note_en"):
            op.add_column("message_enrichment", sa.Column("risk_note_en", sa.String(length=1024), nullable=True))
        if not _has_column("message_enrichment", "enrichment_version"):
            op.add_column(
                "message_enrichment",
                sa.Column("enrichment_version", sa.Integer(), nullable=False, server_default="1"),
            )

    if _has_column("discord_author_profiles", "bio_zh") and not _has_column(
        "discord_author_profiles", "bio_en"
    ):
        op.add_column("discord_author_profiles", sa.Column("bio_en", sa.Text(), nullable=True))

    if _has_column("resonance_signals", "narrative_zh") and not _has_column(
        "resonance_signals", "narrative_en"
    ):
        op.add_column("resonance_signals", sa.Column("narrative_en", sa.Text(), nullable=True))


def downgrade() -> None:
    for table, column in [
        ("message_enrichment", "enrichment_version"),
        ("message_enrichment", "risk_note_en"),
        ("message_enrichment", "bullets_en"),
        ("message_enrichment", "summary_en"),
        ("message_enrichment", "title_en"),
        ("discord_author_profiles", "bio_en"),
        ("resonance_signals", "narrative_en"),
    ]:
        if _has_column(table, column):
            op.drop_column(table, column)
