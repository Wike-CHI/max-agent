"""Add three-scope budget policies and per-attempt commitments.

Revision ID: c77b1f2e8a90
Revises: b76a3e4f1290
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c77b1f2e8a90"
down_revision: Union[str, None] = "b76a3e4f1290"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    exact = sa.Numeric(36, 18) if op.get_bind().dialect.name == "postgresql" else sa.String(80)
    op.create_table(
        "budget_policies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ceiling", exact, nullable=False),
        sa.Column("warning_at", exact, nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner", "scope", "scope_id", "unit", "period_start", name="uq_budget_policy_scope_start"),
    )
    op.create_index("ix_budget_policy_lookup", "budget_policies", ["owner", "scope", "scope_id", "unit"])
    op.create_table(
        "budget_reservations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("provider_accounts.id"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("attempt_id", sa.String(36), sa.ForeignKey("task_attempts.id"), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("reserved", exact, nullable=False),
        sa.Column("actual", exact, nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("evidence_ref", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("attempt_id", name="uq_budget_reservations_attempt"),
        sa.UniqueConstraint("idempotency_key", name="uq_budget_reservations_key"),
    )
    op.create_table(
        "budget_holds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("reservation_id", sa.String(36), sa.ForeignKey("budget_reservations.id"), nullable=False),
        sa.Column("policy_id", sa.String(36), sa.ForeignKey("budget_policies.id"), nullable=False),
        sa.Column("committed", exact, nullable=False),
        sa.UniqueConstraint("reservation_id", "policy_id", name="uq_budget_holds_reservation_policy"),
    )


def downgrade() -> None:
    op.drop_table("budget_holds")
    op.drop_table("budget_reservations")
    op.drop_index("ix_budget_policy_lookup", table_name="budget_policies")
    op.drop_table("budget_policies")
