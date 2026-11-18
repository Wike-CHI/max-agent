"""Add versioned usage reports and decimal cost entries.

Revision ID: a40b9c612e74
Revises: f19a0c3d8e21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a40b9c612e74"
down_revision: Union[str, None] = "f19a0c3d8e21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "usage_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("attempt_id", sa.String(length=36), sa.ForeignKey("task_attempts.id"), nullable=False),
        sa.Column("source_event_key", sa.String(length=128), nullable=False),
        sa.Column("contract_version", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("billing_mode", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("compute_seconds", sa.Float(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_event_key", name="uq_usage_records_source_event_key"),
    )
    op.create_index("ix_usage_records_attempt", "usage_records", ["attempt_id"])
    op.create_table(
        "cost_entries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("usage_record_id", sa.String(length=36), sa.ForeignKey("usage_records.id"), nullable=False),
        sa.Column(
            "amount",
            sa.Numeric(36, 18) if op.get_bind().dialect.name == "postgresql" else sa.String(80),
            nullable=False,
        ),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("billing_mode", sa.String(length=32), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("usage_record_id", name="uq_cost_entries_usage_record"),
    )


def downgrade() -> None:
    op.drop_table("cost_entries")
    op.drop_index("ix_usage_records_attempt", table_name="usage_records")
    op.drop_table("usage_records")
