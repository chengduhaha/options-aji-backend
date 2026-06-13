"""drop legacy agent_traces table (ontology observability removed)

Revision ID: 20260613_0002
Revises: 20260613_0001
Create Date: 2026-06-13
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260613_0002"
down_revision = "20260613_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "agent_traces" in existing_tables:
        op.drop_table("agent_traces")


def downgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "agent_traces" not in existing_tables:
        op.create_table(
            "agent_traces",
            sa.Column("trace_id", sa.String(length=128), nullable=False),
            sa.Column("source", sa.String(length=128), nullable=False),
            sa.Column("query", sa.Text(), nullable=False),
            sa.Column("matched_pattern", sa.String(length=128), nullable=True),
            sa.Column("used_objects", sa.JSON(), nullable=False),
            sa.Column("used_relations", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("trace_id"),
        )
        op.create_index("ix_agent_traces_source", "agent_traces", ["source"])
        op.create_index("ix_agent_traces_matched_pattern", "agent_traces", ["matched_pattern"])
        op.create_index("ix_agent_traces_created_at", "agent_traces", ["created_at"])
