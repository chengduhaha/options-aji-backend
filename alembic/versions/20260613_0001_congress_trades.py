"""create congress_trades table

Revision ID: 20260613_0001
Revises: 20260608_0001
Create Date: 2026-06-13
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260613_0001"
down_revision = "20260608_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "congress_trades" not in existing_tables:
        op.create_table(
            "congress_trades",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("member_name", sa.String(length=256), nullable=False),
            sa.Column("chamber", sa.String(length=16), nullable=False),
            sa.Column("symbol", sa.String(length=32), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=True),
            sa.Column("transaction_type", sa.String(length=64), nullable=True),
            sa.Column("amount_range", sa.String(length=128), nullable=True),
            sa.Column("asset_description", sa.String(length=512), nullable=True),
            sa.Column("comment", sa.Text(), nullable=True),
            sa.Column("raw_json", sa.JSON(), nullable=True),
            sa.Column(
                "ingested_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("idx_ct_member_symbol", "congress_trades", ["member_name", "symbol"])
        op.create_index("idx_ct_symbol_date", "congress_trades", ["symbol", "trade_date"])
        op.create_index("idx_ct_trade_date", "congress_trades", ["trade_date"])


def downgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "congress_trades" in existing_tables:
        op.drop_index("idx_ct_trade_date", table_name="congress_trades")
        op.drop_index("idx_ct_symbol_date", table_name="congress_trades")
        op.drop_index("idx_ct_member_symbol", table_name="congress_trades")
        op.drop_table("congress_trades")
