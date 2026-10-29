"""Execution journal — persist events; AER adapter reserved."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.orm import Session

from max_agent.db.models import TaskEventRow, utcnow
from max_agent.models.enums import EventType


class AERAdapter(Protocol):
    """Reserved interface for Agent Execution Record export."""

    def export_event(self, event: TaskEventRow) -> None: ...


class NullAERAdapter:
    def export_event(self, event: TaskEventRow) -> None:
        return None


class Journal:
    def __init__(self, session: Session, aer: AERAdapter | None = None) -> None:
        self.session = session
        self.aer: AERAdapter = aer or NullAERAdapter()

    def append(
        self,
        task_id: str,
        event_type: EventType | str,
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> TaskEventRow:
        row = TaskEventRow(
            task_id=task_id,
            event_type=str(event_type),
            message=message,
            payload=payload or {},
            created_at=utcnow(),
        )
        self.session.add(row)
        self.session.flush()
        self.aer.export_event(row)
        return row

    def list_for_task(self, task_id: str, *, limit: int = 200) -> list[TaskEventRow]:
        return (
            self.session.query(TaskEventRow)
            .filter(TaskEventRow.task_id == task_id)
            .order_by(TaskEventRow.id.asc())
            .limit(limit)
            .all()
        )
