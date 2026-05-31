"""create supply chain graph tables

Revision ID: 20260530_0001
Revises:
Create Date: 2026-05-30
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260530_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "graph_nodes" not in existing_tables:
        op.create_table(
            "graph_nodes",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("node_type", sa.String(length=24), nullable=False),
            sa.Column("ticker", sa.String(length=32), nullable=True),
            sa.Column("market", sa.String(length=16), nullable=True),
            sa.Column("name_zh", sa.Text(), nullable=False),
            sa.Column("name_en", sa.Text(), nullable=True),
            sa.Column("sector", sa.String(length=128), nullable=True),
            sa.Column("is_listed", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("logo_url", sa.Text(), nullable=True),
            sa.Column("attrs", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.CheckConstraint(
                "node_type IN ('company', 'segment', 'industry', 'product')",
                name="ck_graph_nodes_node_type",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("idx_graph_nodes_node_type", "graph_nodes", ["node_type"])
        op.create_index("idx_graph_nodes_sector", "graph_nodes", ["sector"])
        op.create_index(
            "uq_graph_nodes_ticker_market",
            "graph_nodes",
            ["ticker", "market"],
            unique=True,
            postgresql_where=sa.text("ticker IS NOT NULL"),
            sqlite_where=sa.text("ticker IS NOT NULL"),
        )

    if "graph_edges" not in existing_tables:
        op.create_table(
            "graph_edges",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("source_id", sa.String(length=36), nullable=False),
            sa.Column("target_id", sa.String(length=36), nullable=False),
            sa.Column("rel_type", sa.String(length=32), nullable=False),
            sa.Column("direction", sa.String(length=16), nullable=False, server_default="directed"),
            sa.Column("label", sa.Text(), nullable=True),
            sa.Column("semantic", sa.Text(), nullable=True),
            sa.Column("moat_tier", sa.String(length=16), nullable=True),
            sa.Column("weight", sa.Float(), nullable=True),
            sa.Column("attrs", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("confidence", sa.String(length=16), nullable=False, server_default="confirmed"),
            sa.Column("evidence", sa.Text(), nullable=True),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("as_of_date", sa.Date(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.CheckConstraint(
                "rel_type IN ('supplies_to', 'mutual_supply', 'invests_in', 'parent_of', "
                "'has_segment', 'joint_development', 'partnership', 'competitor', "
                "'licenses_to', 'manufactures_for', 'thematic_link')",
                name="ck_graph_edges_rel_type",
            ),
            sa.CheckConstraint(
                "direction IN ('directed', 'bidirectional', 'undirected')",
                name="ck_graph_edges_direction",
            ),
            sa.CheckConstraint(
                "moat_tier IS NULL OR moat_tier IN ('exclusive', 'primary', 'dominant', 'scarce', 'normal')",
                name="ck_graph_edges_moat_tier",
            ),
            sa.CheckConstraint(
                "confidence IN ('confirmed', 'inferred')",
                name="ck_graph_edges_confidence",
            ),
            sa.ForeignKeyConstraint(["source_id"], ["graph_nodes.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["target_id"], ["graph_nodes.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("idx_graph_edges_source_id", "graph_edges", ["source_id"])
        op.create_index("idx_graph_edges_target_id", "graph_edges", ["target_id"])
        op.create_index("idx_graph_edges_rel_type", "graph_edges", ["rel_type"])
        op.create_index("idx_graph_edges_moat_tier", "graph_edges", ["moat_tier"])
        op.create_index(
            "uq_graph_edges_identity",
            "graph_edges",
            ["source_id", "target_id", "rel_type", "label", "as_of_date"],
            unique=True,
        )

    if "graph_views" not in existing_tables:
        op.create_table(
            "graph_views",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("slug", sa.String(length=96), nullable=False),
            sa.Column("title", sa.String(length=256), nullable=False),
            sa.Column("perspective", sa.String(length=32), nullable=False),
            sa.Column("focus_node_id", sa.String(length=36), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.ForeignKeyConstraint(["focus_node_id"], ["graph_nodes.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("slug"),
        )
        op.create_index("idx_graph_views_slug", "graph_views", ["slug"], unique=True)
        op.create_index("idx_graph_views_perspective", "graph_views", ["perspective"])


def downgrade() -> None:
    op.drop_index("idx_graph_views_perspective", table_name="graph_views")
    op.drop_index("idx_graph_views_slug", table_name="graph_views")
    op.drop_table("graph_views")
    op.drop_index("uq_graph_edges_identity", table_name="graph_edges")
    op.drop_index("idx_graph_edges_moat_tier", table_name="graph_edges")
    op.drop_index("idx_graph_edges_rel_type", table_name="graph_edges")
    op.drop_index("idx_graph_edges_target_id", table_name="graph_edges")
    op.drop_index("idx_graph_edges_source_id", table_name="graph_edges")
    op.drop_table("graph_edges")
    op.drop_index("uq_graph_nodes_ticker_market", table_name="graph_nodes")
    op.drop_index("idx_graph_nodes_sector", table_name="graph_nodes")
    op.drop_index("idx_graph_nodes_node_type", table_name="graph_nodes")
    op.drop_table("graph_nodes")
