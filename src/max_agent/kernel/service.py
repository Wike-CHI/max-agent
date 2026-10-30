"""Single write path for task state — the kernel façade."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from max_agent.db.models import TaskRow, utcnow
from max_agent.kernel.dag import assert_acyclic, dependency_edges, deps_satisfied
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
    GraphNode,
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

    def list_tasks(
        self, *, status: TaskStatus | None = None, limit: int = 100, created_by: str | None = None
    ) -> list[TaskRead]:
        q = self.session.query(TaskRow).order_by(TaskRow.created_at.desc())
        if status is not None:
            q = q.filter(TaskRow.status == status.value)
        if created_by is not None:
            q = q.filter(TaskRow.created_by == created_by)
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

    def create(self, data: TaskCreate, *, created_by: str = "user") -> TaskRead:
        if data.idempotency_key:
            existing = self._idempotent_hit(data.idempotency_key, created_by=created_by)
            if existing is not None:
                return existing

        for dependency in data.dependencies:
            existing = self.session.get(TaskRow, str(dependency))
            if existing is not None and existing.created_by != created_by:
                raise PermissionError("task access denied")

        task_id = str(uuid.uuid4())
        edges = dependency_edges(self.session)
        edges[task_id] = [str(item) for item in data.dependencies]
        assert_acyclic(edges)
        row = self._insert_new(task_id, data, [str(item) for item in data.dependencies], created_by=created_by)
        self.session.flush()
        self._apply_initial_status(row)
        self.session.flush()
        return self._to_read(row)

    def create_graph(self, nodes: list[GraphNode], *, created_by: str = "user") -> list[TaskRead]:
        """Insert a DAG. A cycle or unknown key stores nothing."""
        if not nodes:
            raise ValueError("graph has no nodes")
        keys = [node.key for node in nodes]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate graph key")
        for node in nodes:
            if node.idempotency_key:
                existing = (
                    self.session.query(TaskRow)
                    .filter(TaskRow.idempotency_key == node.idempotency_key)
                    .one_or_none()
                )
                if existing is not None:
                    raise ValueError(f"idempotency key already used: {node.idempotency_key}")

        key_to_id = {node.key: str(uuid.uuid4()) for node in nodes}
        resolved: dict[str, list[str]] = {}
        for node in nodes:
            task_id = key_to_id[node.key]
            deps: list[str] = []
            for dep in node.depends_on:
                if dep in key_to_id:
                    deps.append(key_to_id[dep])
                    continue
                existing = self.session.get(TaskRow, dep)
                if existing is None:
                    raise ValueError(f"unknown dependency: {dep}")
                if existing.created_by != created_by:
                    raise PermissionError("task access denied")
                deps.append(dep)
            resolved[task_id] = deps

        edges = dependency_edges(self.session)
        edges.update(resolved)
        assert_acyclic(edges)

        rows: list[TaskRow] = []
        for node in nodes:
            task_id = key_to_id[node.key]
            rows.append(self._insert_new(task_id, node.to_create(), resolved[task_id], created_by=created_by))
        self.session.flush()
        for row in rows:
            self._apply_initial_status(row)
        self.session.flush()
        return [self._to_read(row) for row in rows]

    def cancel(self, task_id: str) -> TaskRead:
        row = self._get_row(task_id)
        status = TaskStatus(row.status)
        if status in TERMINAL:
            raise ValueError(f"cannot cancel terminal task in status={status}")
        requested = utcnow()
        self._transition(
            row,
            TaskStatus.CANCELLED,
            "cancelled by user",
            cancel_requested_at=requested,
            leased_by=None,
            lease_expires_at=None,
        )
        self.journal.append(
            task_id,
            EventType.CANCELLED,
            "task cancelled",
            {"signal": "cancel", "at": requested.isoformat()},
        )
        self.session.flush()
        return self._to_read(row)

    def approve(
        self,
        task_id: str,
        *,
        actor: str = "user",
        note: str = "",
        scope: list[str] | None = None,
        ttl_seconds: int | None = None,
    ) -> TaskRead:
        """Record an explicit user grant. Sensitive tags cannot run without it.

        `scope` must cover every sensitive tag. An empty scope means those tags,
        or `run` when the task has none. `ttl_seconds` sets the grant expiry.
        """
        row = self._get_row(task_id)
        status = TaskStatus(row.status)
        if status in {TaskStatus.CANCELLED, TaskStatus.SUCCEEDED}:
            raise ValueError(f"cannot approve task in status={status.value}")
        if status in {TaskStatus.LEASED, TaskStatus.RUNNING, TaskStatus.VERIFYING}:
            raise ValueError(f"cannot approve in-flight task status={status.value}")

        actor_name = (actor or "user").strip()[:128] or "user"
        note_value = note.strip() if note else None
        required = sensitive_tags(list(row.tags or []))
        chosen = [item.strip().lower() for item in (scope or []) if item and item.strip()]
        if not chosen:
            chosen = list(required) or ["run"]
        if required and not set(required).issubset(set(chosen)):
            missing = sorted(set(required) - set(chosen))
            raise ValueError(f"approval scope does not cover sensitive tags: {missing}")
        if ttl_seconds is not None and ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")
        now = utcnow()
        extra = {
            "approved_at": now,
            "approved_by": actor_name,
            "approval_note": note_value,
            "approval_scope": chosen,
            "approval_expires_at": None if ttl_seconds is None else now + timedelta(seconds=ttl_seconds),
            "approval_decision": "granted",
            "last_error": None,
        }
        if status in {TaskStatus.BLOCKED, TaskStatus.WAITING_APPROVAL} and deps_satisfied(self.session, row):
            transition_task(
                self.session,
                row,
                TaskStatus.READY,
                f"approved by {actor_name}",
                extra=extra,
                journal=self.journal,
            )
        elif not cas_update(self.session, row, extra):
            raise LeaseHeldError(f"concurrent update on task {task_id}")
        self.journal.append(
            task_id,
            EventType.APPROVED,
            f"approved by {actor_name}",
            {"actor": actor_name, "note": note_value or "", "scope": chosen, "ttl_seconds": ttl_seconds},
        )
        self.session.flush()
        return self._to_read(row)

    def reject(self, task_id: str, *, actor: str = "user", note: str = "") -> TaskRead:
        """Revoke execution. A later approve() is a new grant; this call never runs a worker."""
        row = self._get_row(task_id)
        status = TaskStatus(row.status)
        if status in {TaskStatus.CANCELLED, TaskStatus.SUCCEEDED}:
            raise ValueError(f"cannot reject task in status={status.value}")
        if status in {TaskStatus.LEASED, TaskStatus.RUNNING, TaskStatus.VERIFYING}:
            raise ValueError(f"cannot reject in-flight task status={status.value}")

        actor_name = (actor or "user").strip()[:128] or "user"
        note_value = note.strip() if note else None
        extra = {
            "approval_decision": "rejected",
            "approved_at": None,
            "approved_by": None,
            "approval_note": note_value,
            "approval_expires_at": None,
            "last_error": "approval rejected",
        }
        if status != TaskStatus.FAILED and deps_satisfied(self.session, row):
            target = TaskStatus.WAITING_APPROVAL
        elif status != TaskStatus.FAILED and not deps_satisfied(self.session, row):
            target = TaskStatus.BLOCKED
        else:
            target = status
        if target == status:
            if not cas_update(self.session, row, extra):
                raise LeaseHeldError(f"concurrent update on task {task_id}")
        else:
            transition_task(
                self.session,
                row,
                target,
                "approval rejected",
                extra=extra,
                journal=self.journal,
            )
        self.journal.append(
            task_id,
            EventType.APPROVAL_REJECTED,
            "approval rejected",
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

    def _idempotent_hit(self, key: str, *, created_by: str) -> TaskRead | None:
        existing = self.session.query(TaskRow).filter(TaskRow.idempotency_key == key).one_or_none()
        if existing is None:
            return None
        if existing.created_by != created_by:
            raise PermissionError("task access denied")
        self.journal.append(
            existing.id,
            EventType.IDEMPOTENT_HIT,
            "create ignored; returning existing task",
            {"idempotency_key": key},
        )
        self.session.flush()
        replay = self._to_read(existing)
        replay.idempotent_replay = True
        return replay

    def _insert_new(
        self, task_id: str, data: TaskCreate, dependencies: list[str], *, created_by: str
    ) -> TaskRow:
        budget = data.budget
        timeout = data.timeout_seconds if data.timeout_seconds is not None else budget.timeout_seconds
        issue_ref = explicit_issue_ref(data.issue_ref, data.tags)
        row = TaskRow(
            id=task_id,
            goal=data.goal,
            created_by=created_by,
            acceptance_criteria=list(data.acceptance_criteria),
            priority=data.priority,
            dependencies=list(dependencies),
            status=TaskStatus.PENDING.value,
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
            approval_scope=[],
            created_at=utcnow(),
            updated_at=utcnow(),
            version=1,
        )
        self.session.add(row)
        self.session.flush()
        self.journal.append(
            task_id,
            EventType.CREATED,
            "task created status=pending",
            {
                "goal": data.goal,
                "worker_kind": data.worker_kind.value,
                "issue_ref": issue_ref,
                "budget_ceilings": ceilings_payload(budget.ceilings),
                "max_cost_units": budget.max_cost_units,
                "dependencies": list(dependencies),
            },
        )
        return row

    def _apply_initial_status(self, row: TaskRow) -> None:
        if list(row.dependencies or []) and not deps_satisfied(self.session, row):
            self._transition(row, TaskStatus.BLOCKED, "waiting on dependencies")
        elif TaskStatus(row.status) == TaskStatus.PENDING:
            self._transition(row, TaskStatus.READY, "no dependencies; marked ready")

        found = sensitive_tags(list(row.tags or []))
        if not found:
            return
        reason = f"sensitive tags require approval: {found}"
        if TaskStatus(row.status) == TaskStatus.BLOCKED:
            if not cas_update(self.session, row, {"last_error": reason}):
                raise LeaseHeldError(f"concurrent update on task {row.id}")
        else:
            self._transition(row, TaskStatus.WAITING_APPROVAL, reason, last_error=reason)
        self.journal.append(row.id, EventType.APPROVAL_REQUIRED, reason, {"tags": found})

    def _transition(self, row: TaskRow, target: TaskStatus, message: str, **extra: Any) -> None:
        transition_task(self.session, row, target, message, extra=extra or None, journal=self.journal)

    def _get_row(self, task_id: str) -> TaskRow:
        row = self.session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        return row

    def _to_read(self, row: TaskRow) -> TaskRead:
        return task_to_read(row)
