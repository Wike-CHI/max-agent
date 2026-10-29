"""Single write path for task state — the kernel façade."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from max_agent.db.models import TaskRow, utcnow
from max_agent.kernel.economics import build_trace, compute_metrics
from max_agent.kernel.errors import LeaseHeldError
from max_agent.kernel.journal import Journal
from max_agent.kernel.mutate import cas_update, transition_task
from max_agent.kernel.readmodel import task_to_read
from max_agent.kernel.state_machine import TERMINAL
from max_agent.models.aer import (
    EconomicsReport,
    ExportRetryResult,
    TaskTrace,
    ceilings_payload,
    explicit_issue_ref,
)
from max_agent.models.enums import EventType, TaskStatus, WorkerKind
from max_agent.models.schemas import (
    ScheduleRequest,
    TaskCreate,
    TaskEventRead,
    TaskRead,
)
from max_agent.policy.approval import sensitive_tags


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

    def trace(self, task_id: str) -> TaskTrace:
        return build_trace(self.session, self._get_row(task_id))

    def metrics(self) -> EconomicsReport:
        return compute_metrics(self.session)

    def retry_aer_exports(self) -> ExportRetryResult:
        return self.journal.retry_exports()

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
                replay = self._to_read(existing)
                replay.idempotent_replay = True
                return replay

        budget = data.budget
        timeout = data.timeout_seconds if data.timeout_seconds is not None else budget.timeout_seconds
        issue_ref = explicit_issue_ref(data.issue_ref, data.tags)
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
            issue_ref=issue_ref,
            budget_ceilings=ceilings_payload(budget.ceilings),
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
            {
                "goal": data.goal,
                "worker_kind": data.worker_kind.value,
                "issue_ref": issue_ref,
                "budget_ceilings": ceilings_payload(budget.ceilings),
                "max_cost_units": budget.max_cost_units,
            },
        )

        if initial == TaskStatus.PENDING and not data.dependencies:
            self._transition(row, TaskStatus.READY, "no dependencies; marked ready")

        found = sensitive_tags(list(data.tags))
        if found:
            reason = f"sensitive tags require approval: {found}"
            if TaskStatus(row.status) != TaskStatus.BLOCKED:
                self._transition(row, TaskStatus.BLOCKED, reason, last_error=reason)
            else:
                row.last_error = reason
            self.journal.append(
                task_id,
                EventType.APPROVAL_REQUIRED,
                reason,
                {"tags": found},
            )

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

    def approve(self, task_id: str, *, actor: str = "user", note: str = "") -> TaskRead:
        """Record an explicit user grant. Sensitive tags cannot run without it."""
        row = self._get_row(task_id)
        status = TaskStatus(row.status)
        if status in {TaskStatus.CANCELLED, TaskStatus.SUCCEEDED}:
            raise ValueError(f"cannot approve task in status={status.value}")
        if status in {TaskStatus.LEASED, TaskStatus.RUNNING, TaskStatus.VERIFYING}:
            raise ValueError(f"cannot approve in-flight task status={status.value}")

        actor_name = (actor or "user").strip()[:128] or "user"
        note_value = note.strip() if note else None
        already = row.approved_at is not None
        extra = {
            "approved_at": utcnow(),
            "approved_by": actor_name,
            "approval_note": note_value,
            "last_error": None,
        }
        changed = False
        if status == TaskStatus.BLOCKED and self._deps_satisfied(row):
            transition_task(
                self.session,
                row,
                TaskStatus.READY,
                f"approved by {actor_name}",
                extra=extra,
                journal=self.journal,
            )
            changed = True
        elif not already:
            if not cas_update(self.session, row, extra):
                raise LeaseHeldError(f"concurrent update on task {task_id}")
            changed = True
        if changed:
            self.journal.append(
                task_id,
                EventType.APPROVED,
                f"approved by {actor_name}",
                {"actor": actor_name, "note": note_value or ""},
            )
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

    def _deps_satisfied(self, row: TaskRow) -> bool:
        for dep_id in list(row.dependencies or []):
            dep = self.session.get(TaskRow, dep_id)
            if dep is None or TaskStatus(dep.status) != TaskStatus.SUCCEEDED:
                return False
        return True

    def _get_row(self, task_id: str) -> TaskRow:
        row = self.session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        return row

    def _to_read(self, row: TaskRow) -> TaskRead:
        return task_to_read(row)
