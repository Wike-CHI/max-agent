"""task attempts, fencing token, and retry schedule

Revision ID: b7e1c2a94f30
Revises: 034b97be492a
Create Date: 2026-10-29 18:10:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7e1c2a94f30"
down_revision: Union[str, None] = "034b97be492a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("fencing_token", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("failure_class", sa.String(length=32), nullable=True))
    op.create_index("ix_tasks_status_lease", "tasks", ["status", "lease_expires_at"], unique=False)
    op.create_table(
        "task_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_class", sa.String(length=32), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_ok", sa.Boolean(), nullable=True),
        sa.Column("recoverable", sa.Boolean(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "attempt_no", name="uq_task_attempts_task_no"),
    )
    op.create_index("ix_task_attempts_task_fence", "task_attempts", ["task_id", "fencing_token"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_task_attempts_task_fence", table_name="task_attempts")
    op.drop_table("task_attempts")
    op.drop_index("ix_tasks_status_lease", table_name="tasks")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("failure_class")
        batch_op.drop_column("next_retry_at")
        batch_op.drop_column("fencing_token")
