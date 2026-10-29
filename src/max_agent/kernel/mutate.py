"""Optimistic version updates. Status changes commit only the caller's transaction."""

from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from max_agent.db.models import TaskRow, utcnow
from max_agent.kernel.errors import LeaseHeldError
from max_agent.kernel.journal import Journal
from max_agent.kernel.state_machine import assert_transition
from max_agent.models.enums import EventType, TaskStatus


def cas_update(session: Session, row: TaskRow, values: dict[str, Any]) -> bool:
    """UPDATE ... WHERE id AND version AND status. Returns False if another writer won."""
    current_version = int(row.version or 1)
    current_status = row.status
    payload = dict(values)
    payload["version"] = current_version + 1
    payload["updated_at"] = utcnow()
    result = session.execute(
        update(TaskRow)
        .where(
            TaskRow.id == row.id,
            TaskRow.version == current_version,
            TaskRow.status == current_status,
        )
        .values(**payload)
    )
    if result.rowcount != 1:
        return False
    session.expire(row)
    session.refresh(row)
    return True


def transition_task(
    session: Session,
    row: TaskRow,
    target: TaskStatus,
    message: str,
    *,
    extra: dict[str, Any] | None = None,
    journal: Journal | None = None,
) -> TaskRow:
    current = TaskStatus(row.status)
    values = dict(extra or {})
    if current != target:
        assert_transition(current, target)
        values["status"] = target.value
    elif not values:
        return row
    if not cas_update(session, row, values):
        raise LeaseHeldError(f"concurrent update on task {row.id}")
    if current != target:
        writer = journal or Journal(session)
        writer.append(
            row.id,
            EventType.STATUS_CHANGED,
            message,
            {"from": current.value, "to": target.value, "version": row.version},
        )
    return row
