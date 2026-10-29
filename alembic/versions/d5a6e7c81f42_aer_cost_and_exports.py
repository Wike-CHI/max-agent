"""aer export outbox and per-attempt cost readings

Revision ID: d5a6e7c81f42
Revises: c4a91e0b7d55
Create Date: 2026-10-29 23:40:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5a6e7c81f42"
down_revision: Union[str, None] = "c4a91e0b7d55"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("issue_ref", sa.String(length=256), nullable=True))
        batch_op.add_column(
            sa.Column("budget_ceilings", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
        )
    with op.batch_alter_table("task_attempts") as batch_op:
        batch_op.add_column(sa.Column("worker_kind", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("model", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("provider", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("runtime_ms", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("cost_source", sa.String(length=32), nullable=False, server_default=sa.text("'unknown'"))
        )
        batch_op.add_column(
            sa.Column("cost_marker", sa.String(length=32), nullable=False, server_default=sa.text("'unknown'"))
        )
        batch_op.add_column(sa.Column("cost_amount", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("cost_currency", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("input_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("output_tokens", sa.Integer(), nullable=True))
    op.create_table(
        "aer_exports",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("contract_version", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exported_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["task_events.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_aer_exports_event_id"),
    )
    op.create_index("ix_aer_exports_status", "aer_exports", ["status"], unique=False)
    op.create_index("ix_aer_exports_task", "aer_exports", ["task_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_aer_exports_task", table_name="aer_exports")
    op.drop_index("ix_aer_exports_status", table_name="aer_exports")
    op.drop_table("aer_exports")
    with op.batch_alter_table("task_attempts") as batch_op:
        batch_op.drop_column("output_tokens")
        batch_op.drop_column("input_tokens")
        batch_op.drop_column("cost_currency")
        batch_op.drop_column("cost_amount")
        batch_op.drop_column("cost_marker")
        batch_op.drop_column("cost_source")
        batch_op.drop_column("runtime_ms")
        batch_op.drop_column("provider")
        batch_op.drop_column("model")
        batch_op.drop_column("worker_kind")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("budget_ceilings")
        batch_op.drop_column("issue_ref")
