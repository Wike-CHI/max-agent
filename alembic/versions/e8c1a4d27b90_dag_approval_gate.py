"""approval scope, expiry, decision, and cancel signal

Revision ID: e8c1a4d27b90
Revises: d5a6e7c81f42
Create Date: 2026-10-29 16:40:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8c1a4d27b90"
down_revision: Union[str, None] = "d5a6e7c81f42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(
            sa.Column("approval_scope", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch_op.add_column(sa.Column("approval_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("approval_decision", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("cancel_requested_at")
        batch_op.drop_column("approval_decision")
        batch_op.drop_column("approval_expires_at")
        batch_op.drop_column("approval_scope")
