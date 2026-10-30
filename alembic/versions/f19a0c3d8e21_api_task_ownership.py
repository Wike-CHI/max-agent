"""persist task owners for HTTP authorization

Revision ID: f19a0c3d8e21
Revises: e8c1a4d27b90
Create Date: 2026-10-30 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f19a0c3d8e21"
down_revision: Union[str, None] = "e8c1a4d27b90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(
            sa.Column("created_by", sa.String(length=128), nullable=False, server_default="user")
        )
        batch_op.create_index("ix_tasks_created_by", ["created_by"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_index("ix_tasks_created_by")
        batch_op.drop_column("created_by")
