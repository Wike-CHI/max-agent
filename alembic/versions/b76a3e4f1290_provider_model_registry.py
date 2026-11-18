"""Add owner-scoped provider accounts, model profiles and append-only prices.

Revision ID: b76a3e4f1290
Revises: a40b9c612e74
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b76a3e4f1290"
down_revision: Union[str, None] = "a40b9c612e74"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "provider_accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("account_key", sa.String(64), nullable=False),
        sa.Column("provider_key", sa.String(64), nullable=False),
        sa.Column("billing_mode", sa.String(32), nullable=False),
        sa.Column("credential_ref", sa.String(256), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner", "account_key", name="uq_provider_accounts_owner_key"),
    )
    op.create_index("ix_provider_accounts_owner_provider", "provider_accounts", ["owner", "provider_key"])
    op.create_table(
        "model_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider_key", sa.String(64), nullable=False),
        sa.Column("model_key", sa.String(128), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("supported_efforts", sa.JSON(), nullable=False),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider_key", "model_key", name="uq_model_profiles_provider_model"),
    )
    exact = sa.Numeric(36, 18) if op.get_bind().dialect.name == "postgresql" else sa.String(80)
    op.create_table(
        "price_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("provider_accounts.id"), nullable=False),
        sa.Column("model_id", sa.String(36), sa.ForeignKey("model_profiles.id"), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pricing_unit", sa.String(32), nullable=False),
        sa.Column("currency", sa.String(16), nullable=False),
        sa.Column("input_rate", exact, nullable=True),
        sa.Column("output_rate", exact, nullable=True),
        sa.Column("cached_input_rate", exact, nullable=True),
        sa.Column("compute_rate", exact, nullable=True),
        sa.Column("source", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "model_id", "effective_from", name="uq_price_versions_scope_start"),
    )
    op.create_index("ix_price_versions_scope_start", "price_versions", ["account_id", "model_id", "effective_from"])
    with op.batch_alter_table("cost_entries") as batch_op:
        batch_op.add_column(sa.Column(
            "price_version_id", sa.String(36),
            sa.ForeignKey("price_versions.id", name="fk_cost_entries_price_version"), nullable=True,
        ))


def downgrade() -> None:
    with op.batch_alter_table("cost_entries") as batch_op:
        batch_op.drop_constraint("fk_cost_entries_price_version", type_="foreignkey")
        batch_op.drop_column("price_version_id")
    op.drop_index("ix_price_versions_scope_start", table_name="price_versions")
    op.drop_table("price_versions")
    op.drop_table("model_profiles")
    op.drop_index("ix_provider_accounts_owner_provider", table_name="provider_accounts")
    op.drop_table("provider_accounts")
