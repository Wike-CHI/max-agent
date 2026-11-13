"""add tasks.awaiting_dispatch column

Revision ID: g0b2d4e15c33
Revises: f19a0c3d8e21
Create Date: 2026-11-13 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "g0b2d4e15c33"
down_revision: Union[str, None] = "f19a0c3d8e21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "awaiting_dispatch",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("awaiting_dispatch")
