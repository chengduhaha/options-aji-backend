"""commercial entitlements and Creem webhook events

Revision ID: 20260606_0001
Revises: 20260530_0001
Create Date: 2026-06-06
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260606_0001"
down_revision = "20260530_0001"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "payment_webhook_events" not in existing_tables:
        op.create_table(
            "payment_webhook_events",
            sa.Column("id", sa.String(length=128), nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("event_type", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "idx_payment_webhook_provider_received",
            "payment_webhook_events",
            ["provider", "received_at"],
        )

    if "api_entitlements" in existing_tables:
        columns = [
            ("user_id", sa.Column("user_id", sa.String(length=36), nullable=True)),
            ("provider", sa.Column("provider", sa.String(length=32), nullable=True)),
            ("provider_customer_id", sa.Column("provider_customer_id", sa.String(length=128), nullable=True)),
            ("provider_subscription_id", sa.Column("provider_subscription_id", sa.String(length=128), nullable=True)),
            ("provider_status", sa.Column("provider_status", sa.String(length=32), nullable=True)),
            ("provider_price_id", sa.Column("provider_price_id", sa.String(length=128), nullable=True)),
            (
                "cancel_at_period_end",
                sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
            ),
            ("past_due_since", sa.Column("past_due_since", sa.DateTime(timezone=True), nullable=True)),
            (
                "updated_at",
                sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            ),
        ]
        for name, column in columns:
            if not _has_column("api_entitlements", name):
                op.add_column("api_entitlements", column)

        op.create_index(
            "idx_api_entitlements_user_provider",
            "api_entitlements",
            ["user_id", "provider"],
            if_not_exists=True,
        )
        op.create_index(
            "idx_api_entitlements_provider_customer",
            "api_entitlements",
            ["provider", "provider_customer_id"],
            if_not_exists=True,
        )
        op.create_index(
            "idx_api_entitlements_provider_subscription",
            "api_entitlements",
            ["provider", "provider_subscription_id"],
            if_not_exists=True,
        )


def downgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "api_entitlements" in existing_tables:
        op.drop_index("idx_api_entitlements_provider_subscription", table_name="api_entitlements", if_exists=True)
        op.drop_index("idx_api_entitlements_provider_customer", table_name="api_entitlements", if_exists=True)
        op.drop_index("idx_api_entitlements_user_provider", table_name="api_entitlements", if_exists=True)
        for name in (
            "updated_at",
            "past_due_since",
            "cancel_at_period_end",
            "provider_price_id",
            "provider_status",
            "provider_subscription_id",
            "provider_customer_id",
            "provider",
            "user_id",
        ):
            if _has_column("api_entitlements", name):
                op.drop_column("api_entitlements", name)

    if "payment_webhook_events" in existing_tables:
        op.drop_index("idx_payment_webhook_provider_received", table_name="payment_webhook_events")
        op.drop_table("payment_webhook_events")
