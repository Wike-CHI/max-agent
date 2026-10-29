"""approval grant columns and inbound delivery dedup

Revision ID: c4a91e0b7d55
Revises: b7e1c2a94f30
Create Date: 2026-10-29 22:30:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4a91e0b7d55"
down_revision: Union[str, None] = "b7e1c2a94f30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("approved_by", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("approval_note", sa.Text(), nullable=True))
    op.create_table(
        "inbound_deliveries",
        sa.Column("delivery_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("body_sha256", sa.String(length=64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("delivery_id"),
    )
    op.create_index("ix_inbound_idempotency", "inbound_deliveries", ["idempotency_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_inbound_idempotency", table_name="inbound_deliveries")
    op.drop_table("inbound_deliveries")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("approval_note")
        batch_op.drop_column("approved_by")
        batch_op.drop_column("approved_at")
