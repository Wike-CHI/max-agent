"""Single write path for task state — the kernel façade."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any, Mapping

from sqlalchemy.orm import Session

from max_agent.db.models import TaskRow, utcnow
from max_agent.kernel.dag import assert_acyclic, dependency_edges, deps_satisfied
from max_agent.kernel.economics import build_trace, compute_metrics
from max_agent.kernel.errors import LeaseHeldError
from max_agent.kernel.journal import Journal
from max_agent.kernel.mutate import cas_update, transition_task
from max_agent.kernel.readmodel import task_to_read
from max_agent.kernel.query import latest_verification
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

HASH_FIELDS: tuple[str, ...] = (
    "goal",
    "acceptance_criteria",
    "worker_kind",
    "worker_payload",
    "tags",
    "dependencies",
    "has_side_effects",
    "issue_ref",
    "priority",
    "timeout_seconds",
    "max_attempts",
    "max_cost_units",
    "allow_auto_retry_side_effects",
)
_BOOL_HASH_FIELDS = {"has_side_effects", "allow_auto_retry_side_effects"}
_LIST_HASH_FIELDS = {"tags", "dependencies"}
_BUDGET_HASH_FIELDS = {"max_cost_units", "allow_auto_retry_side_effects"}

# States a task can still be in when its dispatch gate was already cleared.
# Re-dispatching such a task is the idempotent success path (no version bump);
# every other state (cancelled, terminal, in flight) is a conflict.
DISPATCH_REPLAYABLE_STATES = frozenset(
    {TaskStatus.BLOCKED, TaskStatus.WAITING_APPROVAL, TaskStatus.READY}
)


class TaskStateConflict(ValueError):
    """状态 / 版本 / 哈希不匹配；HTTP 层映射为 409。"""


def canonical_json(payload: Mapping[str, Any]) -> bytes:
    """UTF-8、键按字典序排序、无多余空白的 JSON。"""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _field(source: Any, name: str) -> Any:
    budget = None
    if isinstance(source, Mapping):
        if name in source and name not in _BUDGET_HASH_FIELDS:
            value = source[name]
        else:
            budget = source.get("budget")
            if name in _BUDGET_HASH_FIELDS and budget is not None:
                if isinstance(budget, Mapping):
                    value = budget.get(name, source.get(name))
                else:
                    value = getattr(budget, name, source.get(name))
            else:
                value = source.get(name)
    else:
        if name in _BUDGET_HASH_FIELDS and getattr(source, "budget", None) is not None:
            budget = source.budget
            if isinstance(budget, Mapping):
                value = budget.get(name, getattr(source, name, None))
            else:
                value = getattr(budget, name, getattr(source, name, None))
        else:
            value = getattr(source, name, None)
    if hasattr(value, "value") and not isinstance(value, (str, bytes, bool, int, float, list, dict)):
        value = value.value
    return value


def content_hash_of(source: Any) -> str:
    """13 个固定字段的 SHA-256 小写 hex；version / id / status 不参与。"""
    material: dict[str, Any] = {}
    for name in HASH_FIELDS:
        value = _field(source, name)
        if name in _LIST_HASH_FIELDS:
            value = list(value or [])
        elif name in _BOOL_HASH_FIELDS:
            value = bool(value)
        material[name] = value
    return hashlib.sha256(canonical_json(material)).hexdigest()


def task_content_hash(task: Any) -> str:
    return content_hash_of(task)


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

    def latest_verification(self, task_id: str) -> dict | None:
        self._get_row(task_id)
        return latest_verification(self.session, task_id)

    def metrics(self, *, created_by: str | None = None) -> EconomicsReport:
        return compute_metrics(self.session, created_by=created_by)

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
        if not bool(getattr(data, "require_dispatch", False)):
            # A dispatch-gated task stays pending until it is dispatched. It must
            # not become ready (or waiting_approval) just because it was created.
            self._apply_initial_status(row)
        self.session.flush()
        return self._to_read(row)

    def create_graph(
        self,
        nodes: list[GraphNode],
        *,
        created_by: str = "user",
        require_dispatch: bool = False,
    ) -> list[TaskRead]:
        """Insert a DAG. A cycle or unknown key stores nothing.

        `require_dispatch` gates every new node: each task stays pending until
        it is dispatched on its own. A node may also ask for the gate on its own.
        """
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
        gated: set[str] = set()
        for node in nodes:
            task_id = key_to_id[node.key]
            node_gated = bool(require_dispatch) or bool(node.require_dispatch)
            if node_gated:
                gated.add(task_id)
            rows.append(
                self._insert_new(
                    task_id,
                    node.to_create(),
                    resolved[task_id],
                    created_by=created_by,
                    require_dispatch=node_gated,
                )
            )
        self.session.flush()
        for row in rows:
            if row.id in gated:
                # Gated nodes stay pending until they are dispatched.
                continue
            self._apply_initial_status(row)
        self.session.flush()
        return [self._to_read(row) for row in rows]

    def cancel(
        self,
        task_id: str,
        *,
        expected_version: int | None = None,
        content_hash: str | None = None,
        token_id: str | None = None,
    ) -> TaskRead:
        """Cancel a task. A cancelled task stays cancelled.

        `expected_version` and `content_hash` name the exact task state the
        caller acts on: a mismatch is a conflict and the task is left exactly
        as it was. A terminal task (cancelled, succeeded) is a conflict too —
        a replayed cancel never revives it. Without both fields the legacy
        behaviour applies: the task is cancelled without a version check.
        """
        row = self._get_row(task_id)
        status = TaskStatus(row.status)
        if status in TERMINAL:
            # A cancelled task is terminal: cancelling it again is a conflict,
            # not a way back into the state machine.
            raise ValueError(f"cannot cancel terminal task in status={status}")
        self._check_expected_version(row, expected_version)
        self._check_content_hash(row, content_hash)
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
            {"signal": "cancel", "at": requested.isoformat(), "token_id": token_id},
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
        expected_version: int | None = None,
        content_hash: str | None = None,
        token_id: str | None = None,
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
        # A replay of an already applied grant (same token, same command) is a
        # no-op: no CAS write, no version bump, no repeated side effect. A
        # replay never shrinks the recorded grant.
        if self._approve_replay_hit(row, token_id=token_id, content_hash=content_hash, scope=chosen):
            return self._to_read(row)
        self._check_expected_version(row, expected_version)
        self._check_content_hash(row, content_hash)
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
        if (
            status in {TaskStatus.BLOCKED, TaskStatus.WAITING_APPROVAL}
            and not bool(row.awaiting_dispatch)
            and deps_satisfied(self.session, row)
        ):
            # An undispatched task keeps its gate: approval alone must not make
            # it runnable.
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
            {
                "actor": actor_name,
                "note": note_value or "",
                "scope": chosen,
                "ttl_seconds": ttl_seconds,
                "token_id": token_id,
            },
        )
        self.session.flush()
        return self._to_read(row)

    def reject(
        self,
        task_id: str,
        *,
        actor: str = "user",
        note: str = "",
        expected_version: int | None = None,
        content_hash: str | None = None,
        token_id: str | None = None,
    ) -> TaskRead:
        """Revoke execution. A later approve() is a new grant; this call never runs a worker."""
        row = self._get_row(task_id)
        status = TaskStatus(row.status)
        if status in {TaskStatus.CANCELLED, TaskStatus.SUCCEEDED}:
            raise ValueError(f"cannot reject task in status={status.value}")
        if status in {TaskStatus.LEASED, TaskStatus.RUNNING, TaskStatus.VERIFYING}:
            raise ValueError(f"cannot reject in-flight task status={status.value}")

        # A replay of an already applied rejection (same token, same command)
        # is a no-op: no CAS write, no version bump, no repeated side effect.
        if self._reject_replay_hit(row, token_id=token_id, content_hash=content_hash):
            return self._to_read(row)
        self._check_expected_version(row, expected_version)
        self._check_content_hash(row, content_hash)

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
            {"actor": actor_name, "note": note_value or "", "token_id": token_id},
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

    def dispatch(
        self,
        task_id: str,
        *,
        expected_version: int | None = None,
        content_hash: str | None = None,
        token_id: str | None = None,
    ) -> TaskRead:
        """Release a dispatch-gated task into the normal state machine.

        This is a state transition only: no worker runs here, no lease is
        taken and the scheduler is never called. The task leaves the gate and
        then follows the normal rules (dependencies, sensitive tags).

        Two shapes, one contract:

        - The gate is still closed: the caller names the exact state it wants
          to release (`expected_version` + `content_hash`). The gate is
          cleared and the task continues through the normal state machine.
        - The gate is already open: dispatching again is the idempotent
          success path — the same read model, no version bump — but only while
          the task is still in a state a dispatch could have produced
          (`blocked`, `waiting_approval`, `ready`). Anything else (cancelled,
          terminal, in flight) is a conflict: a replay must never revive a
          task that already left the gate.
        """
        row = self._get_row(task_id)
        if not bool(row.awaiting_dispatch):
            # The gate is already open. A wrong hash still means "not this
            # task state". The version is deliberately not checked here: a
            # replay carries the version it saw before the gate was cleared.
            self._check_content_hash(row, content_hash)
            status = TaskStatus(row.status)
            if status not in DISPATCH_REPLAYABLE_STATES:
                raise TaskStateConflict(
                    f"task {row.id} is not dispatchable in status={status.value}"
                )
            return self._to_read(row)

        # The gate is still closed: the caller must describe the exact state it
        # wants to release.
        self._check_expected_version(row, expected_version)
        self._check_content_hash(row, content_hash)

        gate = {"awaiting_dispatch": False}
        if TaskStatus(row.status) not in TERMINAL:
            self._apply_initial_status(row, extra=gate)
        if bool(row.awaiting_dispatch):
            # Nothing transitioned (the task was already past the initial
            # status); clear the gate on its own.
            if not cas_update(self.session, row, gate):
                raise LeaseHeldError(f"concurrent update on task {task_id}")
        self.journal.append(
            task_id,
            EventType.DISPATCHED,
            "task dispatched",
            {
                "signal": "dispatch",
                "token_id": token_id,
                "status": TaskStatus(row.status).value,
            },
        )
        self.session.flush()
        return self._to_read(row)

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

    def _check_expected_version(self, row: TaskRow, expected_version: int | None) -> None:
        """Optimistic concurrency guard: the caller must name the version it saw."""
        if expected_version is None:
            return
        try:
            wanted = int(expected_version)
        except (TypeError, ValueError):
            raise TaskStateConflict(f"expected_version is not an integer on task {row.id}")
        current = int(row.version or 0)
        if wanted != current:
            raise TaskStateConflict(
                f"version conflict on task {row.id}: expected {wanted}, current {current}"
            )

    def _check_content_hash(self, row: TaskRow, content_hash: str | None) -> None:
        """The caller must name the content hash of the task it acts on."""
        if content_hash is None:
            return
        current = content_hash_of(row)
        if str(content_hash) != current:
            raise TaskStateConflict(f"content hash mismatch on task {row.id}")

    def _event_kind(self, event: Any) -> str:
        raw = getattr(event, "event_type", None)
        try:
            return str(EventType(raw).value)
        except (TypeError, ValueError):
            return str(raw)

    def _event_payload(self, event: Any) -> dict[str, Any]:
        payload = getattr(event, "payload", None) or {}
        if isinstance(payload, (bytes, bytearray)):
            try:
                payload = payload.decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                return {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                return {}
        if isinstance(payload, Mapping):
            return dict(payload)
        return {}

    def _event_order(self, event: Any) -> tuple[int, float]:
        created = getattr(event, "created_at", None)
        if created is None:
            return (0, 0.0)
        try:
            return (1, float(created.timestamp()))
        except (AttributeError, TypeError, ValueError, OverflowError, OSError):
            return (0, 0.0)

    def _latest_decision(self, task_id: str) -> tuple[str, dict[str, Any]] | None:
        """The most recent approve/reject decision recorded for a task.

        Only the latest decision may be replayed: a grant that was revoked by a
        later rejection is not a valid replay target anymore.
        """
        try:
            events = list(self.journal.list_for_task(task_id, limit=1000))
        except TypeError:
            events = list(self.journal.list_for_task(task_id))
        kinds = {str(EventType.APPROVED.value), str(EventType.APPROVAL_REJECTED.value)}
        best: tuple[str, dict[str, Any]] | None = None
        best_key: tuple[int, float] | None = None
        for event in events:
            kind = self._event_kind(event)
            if kind not in kinds:
                continue
            key = self._event_order(event)
            if best_key is None or key >= best_key:
                best_key = key
                best = (kind, self._event_payload(event))
        return best

    def _token_matches(self, payload: Mapping[str, Any], token_id: str | None) -> bool:
        if not token_id:
            return False
        recorded = payload.get("token_id")
        if recorded is None:
            return False
        return str(recorded) == str(token_id)

    def _approve_replay_hit(
        self,
        row: TaskRow,
        *,
        token_id: str | None,
        content_hash: str | None,
        scope: list[str],
    ) -> bool:
        """True when this approve command was already applied to this task.

        A replay needs the same token, the same content, a grant that is still
        in force, and — the part that matters — a request scope that the
        recorded grant already covers: `request scope ⊆ granted scope`.
        A wider request scope is a new grant, not a replay, and a narrower one
        never shrinks the grant that is already on the task.
        """
        if not token_id:
            return False
        if content_hash is None or str(content_hash) != content_hash_of(row):
            return False
        if str(getattr(row, "approval_decision", "") or "") != "granted":
            return False
        expires = getattr(row, "approval_expires_at", None)
        if expires is not None:
            try:
                now = utcnow()
                if getattr(expires, "tzinfo", None) is None:
                    expires = expires.replace(tzinfo=now.tzinfo)
                if expires <= now:
                    return False
            except (TypeError, AttributeError):
                pass
        granted = [str(item) for item in (getattr(row, "approval_scope", None) or [])]
        if not set(scope).issubset(set(granted)):
            # The request scope is not covered by the existing grant: this is a
            # different command, not a replay of the one already applied.
            return False
        latest = self._latest_decision(row.id)
        if latest is None:
            return False
        kind, payload = latest
        if kind != str(EventType.APPROVED.value):
            return False
        return self._token_matches(payload, token_id)

    def _reject_replay_hit(
        self,
        row: TaskRow,
        *,
        token_id: str | None,
        content_hash: str | None,
    ) -> bool:
        """True when this reject command was already applied to this task."""
        if not token_id:
            return False
        if content_hash is not None and str(content_hash) != content_hash_of(row):
            return False
        if str(getattr(row, "approval_decision", "") or "") != "rejected":
            return False
        latest = self._latest_decision(row.id)
        if latest is None:
            return False
        kind, payload = latest
        if kind != str(EventType.APPROVAL_REJECTED.value):
            return False
        return self._token_matches(payload, token_id)

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
        self,
        task_id: str,
        data: TaskCreate,
        dependencies: list[str],
        *,
        created_by: str,
        require_dispatch: bool | None = None,
    ) -> TaskRow:
        budget = data.budget
        timeout = data.timeout_seconds if data.timeout_seconds is not None else budget.timeout_seconds
        issue_ref = explicit_issue_ref(data.issue_ref, data.tags)
        gated = bool(getattr(data, "require_dispatch", False)) or bool(require_dispatch)
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
            awaiting_dispatch=gated,
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

    def _apply_initial_status(self, row: TaskRow, *, extra: dict[str, Any] | None = None) -> None:
        """Give a task its starting status.

        `extra` holds column updates that ride along with the transition — the
        dispatch gate uses it to clear `awaiting_dispatch` in the same write.
        """
        gate_extra = dict(extra or {})
        if list(row.dependencies or []) and not deps_satisfied(self.session, row):
            self._transition(row, TaskStatus.BLOCKED, "waiting on dependencies", **gate_extra)
        elif TaskStatus(row.status) == TaskStatus.PENDING:
            self._transition(row, TaskStatus.READY, "no dependencies; marked ready", **gate_extra)

        found = sensitive_tags(list(row.tags or []))
        if not found:
            return
        reason = f"sensitive tags require approval: {found}"
        if TaskStatus(row.status) == TaskStatus.BLOCKED:
            if not cas_update(self.session, row, {"last_error": reason}):
                raise LeaseHeldError(f"concurrent update on task {row.id}")
        else:
            self._transition(row, TaskStatus.WAITING_APPROVAL, reason, last_error=reason, **gate_extra)
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
