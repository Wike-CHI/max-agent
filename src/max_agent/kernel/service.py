"""Single write path for task state — the kernel façade."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from max_agent.db.models import TaskRow, utcnow
from max_agent.kernel.journal import Journal
from max_agent.kernel.mutate import transition_task
from max_agent.kernel.readmodel import task_to_read
from max_agent.kernel.state_machine import TERMINAL
from max_agent.models.enums import EventType, TaskStatus, WorkerKind
from max_agent.models.schemas import (
    ScheduleRequest,
    TaskCreate,
    TaskEventRead,
    TaskRead,
)


class TaskService:
    """All status mutations go through this service (no concurrent overwrite races)."""

    def __init__(self, session: Session, worker_id: str = "worker-1") -> None:
        self.session = session
        self.journal = Journal(session)
        self.worker_id = worker_id

    # ----- reads -----

    def get(self, task_id: str) -> TaskRead:
        row = self._get_row(task_id)
        return self._to_read(row)

    def list_tasks(self, *, status: TaskStatus | None = None, limit: int = 100) -> list[TaskRead]:
        q = self.session.query(TaskRow).order_by(TaskRow.created_at.desc())
        if status is not None:
            q = q.filter(TaskRow.status == status.value)
        return [self._to_read(r) for r in q.limit(limit).all()]

    def list_events(self, task_id: str, *, limit: int = 200) -> list[TaskEventRead]:
        self._get_row(task_id)  # ensure exists
        rows = self.journal.list_for_task(task_id, limit=limit)
        return [
            TaskEventRead(
                id=e.id,
                task_id=e.task_id,
                event_type=EventType(e.event_type),
                message=e.message,
                payload=e.payload or {},
                created_at=e.created_at,
            )
            for e in rows
        ]

    # ----- writes -----

    def create(self, data: TaskCreate) -> TaskRead:
        if data.idempotency_key:
            existing = (
                self.session.query(TaskRow)
                .filter(TaskRow.idempotency_key == data.idempotency_key)
                .one_or_none()
            )
            if existing is not None:
                self.journal.append(
                    existing.id,
                    EventType.IDEMPOTENT_HIT,
                    "create ignored; returning existing task",
                    {"idempotency_key": data.idempotency_key},
                )
                self.session.flush()
                return self._to_read(existing)

        budget = data.budget
        timeout = data.timeout_seconds if data.timeout_seconds is not None else budget.timeout_seconds
        task_id = str(uuid.uuid4())
        initial = TaskStatus.PENDING
        # if deps present → blocked until deps succeed
        if data.dependencies:
            initial = TaskStatus.BLOCKED

        row = TaskRow(
            id=task_id,
            goal=data.goal,
            acceptance_criteria=list(data.acceptance_criteria),
            priority=data.priority,
            dependencies=list(data.dependencies),
            status=initial.value,
            attempts=0,
            max_attempts=budget.max_attempts,
            timeout_seconds=timeout,
            max_cost_units=budget.max_cost_units,
            allow_auto_retry_side_effects=budget.allow_auto_retry_side_effects,
            idempotency_key=data.idempotency_key,
            worker_kind=data.worker_kind.value,
            worker_payload=dict(data.worker_payload),
            has_side_effects=data.has_side_effects,
            tags=list(data.tags),
            created_at=utcnow(),
            updated_at=utcnow(),
            version=1,
        )
        self.session.add(row)
        self.session.flush()
        self.journal.append(
            task_id,
            EventType.CREATED,
            f"task created status={initial.value}",
            {"goal": data.goal, "worker_kind": data.worker_kind.value},
        )

        if initial == TaskStatus.PENDING and not data.dependencies:
            self._transition(row, TaskStatus.READY, "no dependencies; marked ready")

        self.session.flush()
        return self._to_read(row)

    def cancel(self, task_id: str) -> TaskRead:
        row = self._get_row(task_id)
        status = TaskStatus(row.status)
        if status in TERMINAL:
            raise ValueError(f"cannot cancel terminal task in status={status}")
        self._transition(row, TaskStatus.CANCELLED, "cancelled by user")
        self.journal.append(task_id, EventType.CANCELLED, "task cancelled")
        self.session.flush()
        return self._to_read(row)

    def schedule(self, task_id: str, req: ScheduleRequest | None = None) -> TaskRead:
        """Commit this session, then claim/execute/finalize on short transactions.

        Worker execution is not allowed to share this SQLAlchemy session.
        """
        self.session.commit()
        from max_agent.kernel.scheduler import Scheduler

        return Scheduler(self.session, worker_id=self.worker_id).run_one(task_id, req)

    def reopen(self, task_id: str) -> TaskRead:
        """Manual reopen of failed task → ready."""
        row = self._get_row(task_id)
        self._transition(
            row,
            TaskStatus.READY,
            "manually reopened",
            last_error=None,
            failure_class=None,
            leased_by=None,
            lease_expires_at=None,
            next_retry_at=None,
        )
        self.session.flush()
        return self._to_read(row)

    # ----- internals -----

    def _transition(self, row: TaskRow, target: TaskStatus, message: str, **extra: Any) -> None:
        transition_task(self.session, row, target, message, extra=extra or None, journal=self.journal)

    def _get_row(self, task_id: str) -> TaskRow:
        row = self.session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        return row

    def _to_read(self, row: TaskRow) -> TaskRead:
        return task_to_read(row)
