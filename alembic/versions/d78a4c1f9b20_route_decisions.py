"""Persist route decisions bound to one reserved attempt.

Revision ID: d78a4c1f9b20
Revises: c77b1f2e8a90
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d78a4c1f9b20"
down_revision: Union[str, None] = "c77b1f2e8a90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    exact = sa.Numeric(36, 18) if op.get_bind().dialect.name == "postgresql" else sa.String(80)
    op.create_table(
        "route_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("attempt_id", sa.String(36), sa.ForeignKey("task_attempts.id"), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("reservation_id", sa.String(36), sa.ForeignKey("budget_reservations.id"), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("provider_accounts.id"), nullable=False),
        sa.Column("model_id", sa.String(36), sa.ForeignKey("model_profiles.id"), nullable=False),
        sa.Column("price_version_id", sa.String(36), sa.ForeignKey("price_versions.id"), nullable=False),
        sa.Column("policy_version", sa.String(16), nullable=False),
        sa.Column("vendor", sa.String(16), nullable=False),
        sa.Column("model_key", sa.String(128), nullable=False),
        sa.Column("effort", sa.String(32), nullable=True),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("upper_bound", exact, nullable=False),
        sa.Column("policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("attempt_id", name="uq_route_decisions_attempt"),
    )
    if op.get_bind().dialect.name == "sqlite":
        op.execute("CREATE TRIGGER route_decisions_no_update BEFORE UPDATE ON route_decisions "
                   "BEGIN SELECT RAISE(ABORT, 'route decisions are immutable'); END")
        op.execute("CREATE TRIGGER route_decisions_no_delete BEFORE DELETE ON route_decisions "
                   "BEGIN SELECT RAISE(ABORT, 'route decisions are immutable'); END")
    elif op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE FUNCTION route_decisions_immutable() RETURNS trigger AS $$ "
                   "BEGIN RAISE EXCEPTION 'route decisions are immutable'; END; $$ LANGUAGE plpgsql")
        op.execute("CREATE TRIGGER route_decisions_no_update BEFORE UPDATE ON route_decisions "
                   "FOR EACH ROW EXECUTE FUNCTION route_decisions_immutable()")
        op.execute("CREATE TRIGGER route_decisions_no_delete BEFORE DELETE ON route_decisions "
                   "FOR EACH ROW EXECUTE FUNCTION route_decisions_immutable()")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER route_decisions_no_update ON route_decisions")
        op.execute("DROP TRIGGER route_decisions_no_delete ON route_decisions")
        op.execute("DROP FUNCTION route_decisions_immutable()")
    op.drop_table("route_decisions")
