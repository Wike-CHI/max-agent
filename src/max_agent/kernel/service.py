"""Single write path for task state — the kernel façade."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from max_agent.db.models import TaskRow, utcnow
from max_agent.kernel.journal import Journal
from max_agent.kernel.state_machine import TERMINAL, assert_transition
from max_agent.kernel.verification import verify
from max_agent.models.enums import EventType, TaskStatus, WorkerKind
from max_agent.models.schemas import (
    BudgetConstraints,
    ScheduleRequest,
    TaskCreate,
    TaskEventRead,
    TaskRead,
    WorkerResult,
)
from max_agent.policy.approval import requires_human_approval
from max_agent.workers.base import WorkerContext
from max_agent.workers.registry import get_worker


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
        """Claim + run one task with the configured worker (single-worker MVP)."""
        req = req or ScheduleRequest()
        row = self._get_row(task_id)
        status = TaskStatus(row.status)

        if status in TERMINAL:
            raise ValueError(f"cannot schedule terminal task status={status}")

        is_retry = row.attempts > 0 or status == TaskStatus.RETRY_WAIT
        need, reason = requires_human_approval(
            tags=list(row.tags or []),
            has_side_effects=bool(row.has_side_effects),
            is_retry=is_retry,
            confirm_side_effects=req.confirm_side_effects,
            allow_auto_retry_side_effects=bool(row.allow_auto_retry_side_effects),
        )
        if need:
            self._transition(row, TaskStatus.BLOCKED, reason)
            self.journal.append(
                task_id,
                EventType.APPROVAL_REQUIRED,
                reason,
                {"confirm_side_effects": req.confirm_side_effects},
            )
            self.session.flush()
            return self._to_read(row)

        # resolve deps
        if not self._deps_satisfied(row):
            self._transition(row, TaskStatus.BLOCKED, "dependencies not satisfied")
            self.session.flush()
            return self._to_read(row)

        # Move into READY from pending / retry / blocked(after approval)
        cur = TaskStatus(row.status)
        if cur in {TaskStatus.PENDING, TaskStatus.RETRY_WAIT, TaskStatus.BLOCKED}:
            self._transition(row, TaskStatus.READY, f"approved for run from {cur.value}")

        kind = WorkerKind(req.worker_kind.value if req.worker_kind else row.worker_kind)
        row.worker_kind = kind.value

        # lease
        self._transition(row, TaskStatus.LEASED, f"leased by {self.worker_id}")
        row.leased_by = self.worker_id
        row.lease_expires_at = None  # reserved for multi-worker TTL
        self.journal.append(
            task_id,
            EventType.LEASED,
            f"leased by {self.worker_id}",
            {"worker_kind": kind.value},
        )
        self.journal.append(
            task_id,
            EventType.SCHEDULED,
            "schedule accepted",
            {"worker_kind": kind.value},
        )

        # run
        self._transition(row, TaskStatus.RUNNING, "worker started")
        row.attempts += 1
        attempt_no = row.attempts
        self.journal.append(
            task_id,
            EventType.ATTEMPT_STARTED,
            f"attempt {attempt_no} started",
            {"attempt": attempt_no, "worker_kind": kind.value},
        )
        self.session.flush()

        tool_buffer: list[dict[str, Any]] = []

        def on_tool(name: str, args: dict[str, Any]) -> None:
            tool_buffer.append({"tool": name, "args": args})
            self.journal.append(
                task_id,
                EventType.TOOL_CALL,
                name,
                {"args": args, "attempt": attempt_no},
            )

        worker = get_worker(kind)
        ctx = WorkerContext(
            task_id=task_id,
            goal=row.goal,
            acceptance_criteria=list(row.acceptance_criteria or []),
            payload=dict(row.worker_payload or {}),
            timeout_seconds=row.timeout_seconds,
            on_tool_call=on_tool,
        )

        try:
            result = worker.run(ctx)
        except Exception as exc:  # noqa: BLE001 — convert to recoverable worker failure
            result = WorkerResult(
                ok=False,
                summary="worker raised exception",
                outputs={},
                tool_calls=tool_buffer,
                recoverable=True,
                error=str(exc),
            )

        self.journal.append(
            task_id,
            EventType.ATTEMPT_FINISHED,
            result.summary,
            {
                "attempt": attempt_no,
                "ok": result.ok,
                "error": result.error,
                "recoverable": result.recoverable,
            },
        )
        self.journal.append(
            task_id,
            EventType.WORKER_OUTPUT,
            "worker outputs",
            {"outputs": result.outputs, "tool_calls": result.tool_calls or tool_buffer},
        )

        # verify
        self._transition(row, TaskStatus.VERIFYING, "verifying acceptance criteria")
        v = verify(list(row.acceptance_criteria or []), result, payload=dict(row.worker_payload or {}))
        self.journal.append(
            task_id,
            EventType.VERIFICATION,
            v.message,
            {"passed": v.passed, "checks": v.checks},
        )

        if v.passed and result.ok:
            row.result_summary = result.summary
            row.last_error = None
            row.leased_by = None
            self._transition(row, TaskStatus.SUCCEEDED, "accepted")
        else:
            self._handle_failure(row, result, v.message)

        self.session.flush()
        return self._to_read(row)

    def reopen(self, task_id: str) -> TaskRead:
        """Manual reopen of failed task → ready."""
        row = self._get_row(task_id)
        self._transition(row, TaskStatus.READY, "manually reopened")
        row.last_error = None
        self.session.flush()
        return self._to_read(row)

    # ----- internals -----

    def _handle_failure(self, row: TaskRow, result: WorkerResult, verify_msg: str) -> None:
        err = result.error or verify_msg or result.summary
        row.last_error = err
        row.result_summary = result.summary
        row.leased_by = None

        if (not result.recoverable) or row.attempts >= row.max_attempts:
            self._transition(
                row,
                TaskStatus.FAILED,
                f"failed after {row.attempts} attempt(s): {err}",
            )
            self.journal.append(
                row.id,
                EventType.ERROR,
                row.last_error or "failed",
                {"attempts": row.attempts, "max_attempts": row.max_attempts},
            )
            return

        self._transition(row, TaskStatus.RETRY_WAIT, f"recoverable failure: {err}")
        self.journal.append(
            row.id,
            EventType.ERROR,
            f"recoverable; will retry ({row.attempts}/{row.max_attempts})",
            {"error": err},
        )

    def _deps_satisfied(self, row: TaskRow) -> bool:
        deps = list(row.dependencies or [])
        if not deps:
            return True
        for dep_id in deps:
            dep = self.session.get(TaskRow, dep_id)
            if dep is None or TaskStatus(dep.status) != TaskStatus.SUCCEEDED:
                return False
        return True

    def _transition(self, row: TaskRow, target: TaskStatus, message: str) -> None:
        current = TaskStatus(row.status)
        if current == target:
            return
        assert_transition(current, target)
        row.status = target.value
        row.updated_at = utcnow()
        row.version = int(row.version or 1) + 1
        self.journal.append(
            row.id,
            EventType.STATUS_CHANGED,
            message,
            {"from": current.value, "to": target.value, "version": row.version},
        )

    def _get_row(self, task_id: str) -> TaskRow:
        row = self.session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        return row

    def _to_read(self, row: TaskRow) -> TaskRead:
        budget = BudgetConstraints(
            max_attempts=row.max_attempts,
            timeout_seconds=row.timeout_seconds,
            max_cost_units=row.max_cost_units,
            allow_auto_retry_side_effects=row.allow_auto_retry_side_effects,
        )
        return TaskRead(
            id=row.id,
            goal=row.goal,
            acceptance_criteria=list(row.acceptance_criteria or []),
            priority=row.priority,
            dependencies=list(row.dependencies or []),
            status=TaskStatus(row.status),
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            timeout_seconds=row.timeout_seconds,
            budget=budget,
            idempotency_key=row.idempotency_key,
            worker_kind=WorkerKind(row.worker_kind),
            worker_payload=dict(row.worker_payload or {}),
            has_side_effects=bool(row.has_side_effects),
            tags=list(row.tags or []),
            result_summary=row.result_summary,
            last_error=row.last_error,
            created_at=row.created_at,
            updated_at=row.updated_at,
            leased_by=row.leased_by,
            lease_expires_at=row.lease_expires_at,
        )
