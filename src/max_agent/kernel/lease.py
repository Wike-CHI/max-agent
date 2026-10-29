"""Atomic task claim, fencing, and expired-lease recovery.

PostgreSQL takes FOR UPDATE SKIP LOCKED on the candidate row.
SQLite is the single-worker fallback: BEGIN IMMEDIATE (see db.session)
plus a version/status compare-and-set. Either way the lease row is
committed before worker.run() and a later finalize must present the
same fencing token.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from max_agent.db.models import TaskAttemptRow, TaskRow, utcnow
from max_agent.kernel.economics import assess_budget
from max_agent.kernel.errors import ConcurrencyLimitError, LeaseHeldError
from max_agent.kernel.journal import Journal
from max_agent.kernel.mutate import cas_update, transition_task
from max_agent.kernel.readmodel import task_to_read
from max_agent.kernel.retry import backoff_delay_seconds
from max_agent.kernel.timeutil import as_utc
from max_agent.models.enums import AttemptStatus, EventType, FailureClass, TaskStatus
from max_agent.models.schemas import ScheduleRequest, TaskRead
from max_agent.policy.approval import approval_hold

_IN_FLIGHT = frozenset({TaskStatus.LEASED, TaskStatus.RUNNING, TaskStatus.VERIFYING})
_TERMINAL_SCHEDULE = frozenset({TaskStatus.SUCCEEDED, TaskStatus.CANCELLED, TaskStatus.FAILED})


@dataclass(frozen=True)
class AttemptClaim:
    """Snapshot committed before execution. Do not touch the DB with this object."""

    task_id: str
    attempt_id: str
    attempt_no: int
    fencing_token: int
    lease_owner: str
    lease_expires_at: datetime
    goal: str
    acceptance_criteria: tuple[str, ...]
    payload: dict[str, Any]
    timeout_seconds: int
    worker_kind: str
    has_side_effects: bool


def select_task_for_claim(dialect: str, task_id: str):
    """Row lock statement used by the claim path. SQLite omits FOR UPDATE."""
    stmt = select(TaskRow).where(TaskRow.id == task_id)
    if dialect == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    return stmt


def select_task_for_finalize(dialect: str, task_id: str):
    """Finalize waits for the row lock so it observes a concurrent reclaim."""
    stmt = select(TaskRow).where(TaskRow.id == task_id)
    if dialect == "postgresql":
        stmt = stmt.with_for_update()
    return stmt


def _dialect(session: Session) -> str:
    return session.get_bind().dialect.name


def lease_expired(row: TaskRow, now: datetime) -> bool:
    status = TaskStatus(row.status)
    if status not in _IN_FLIGHT:
        return False
    expires = as_utc(row.lease_expires_at)
    # A running row with no expiry cannot be fenced; treat it as already lost.
    if expires is None:
        return True
    return expires <= as_utc(now)  # type: ignore[operator]


def _load(session: Session, task_id: str, *, skip_locked: bool) -> TaskRow:
    dialect = _dialect(session)
    stmt = select_task_for_claim(dialect, task_id) if skip_locked else select_task_for_finalize(dialect, task_id)
    row = session.execute(stmt).scalar_one_or_none()
    if row is not None:
        return row
    exists = session.get(TaskRow, task_id)
    if exists is None:
        raise KeyError(f"task not found: {task_id}")
    raise LeaseHeldError(f"task {task_id} is locked by another worker")


def active_inflight_count(session: Session, now: datetime) -> int:
    """Unexpired leased/running/verifying rows. Expired leases do not hold a slot."""
    rows = session.query(TaskRow).filter(TaskRow.status.in_([status.value for status in _IN_FLIGHT])).all()
    return sum(1 for row in rows if not lease_expired(row, now))


def _close_open_attempts(
    session: Session,
    task_id: str,
    fencing_token: int,
    *,
    reason: str,
    failure_class: FailureClass,
    status: AttemptStatus,
) -> None:
    now = utcnow()
    rows = (
        session.query(TaskAttemptRow)
        .filter(
            TaskAttemptRow.task_id == task_id,
            TaskAttemptRow.fencing_token == fencing_token,
            TaskAttemptRow.finished_at.is_(None),
        )
        .all()
    )
    for attempt in rows:
        attempt.status = status.value
        attempt.failure_class = failure_class.value
        attempt.error = reason
        attempt.finished_at = now
        attempt.worker_ok = False
    session.flush()


def recover_inflight(
    session: Session,
    row: TaskRow,
    *,
    now: datetime,
    confirm_side_effects: bool,
    backoff_base_seconds: float,
    backoff_cap_seconds: float,
) -> TaskRow | None:
    """Move an expired in-flight task to failed, blocked, or retry_wait.

    Returns None when another transaction won the version CAS.
    """
    if not lease_expired(row, now):
        return row
    reason = "lease expired before worker finalize"
    old_token = int(row.fencing_token or 0)
    hold = approval_hold(
        tags=list(row.tags or []),
        has_side_effects=bool(row.has_side_effects),
        is_retry=True,
        confirm_side_effects=confirm_side_effects,
        allow_auto_retry_side_effects=bool(row.allow_auto_retry_side_effects),
        idempotency_key=row.idempotency_key,
        approved_at=row.approved_at,
        approval_decision=row.approval_decision,
        approval_expires_at=row.approval_expires_at,
        approval_scope=list(row.approval_scope or []),
        now=now,
    )
    if int(row.attempts) >= int(row.max_attempts):
        target = TaskStatus.FAILED
        message = f"lease expired and max attempts exhausted: {reason}"
        last_error = message
    elif hold.status is not None:
        target = hold.status
        message = hold.reason
        last_error = hold.reason
    else:
        target = TaskStatus.RETRY_WAIT
        message = reason
        last_error = reason

    extra: dict[str, Any] = {
        "leased_by": None,
        "lease_expires_at": None,
        "failure_class": FailureClass.CRASH.value,
        "last_error": last_error,
    }
    if hold.decision and target == hold.status:
        extra["approval_decision"] = hold.decision
    if target == TaskStatus.RETRY_WAIT:
        delay = backoff_delay_seconds(
            int(row.attempts),
            base=backoff_base_seconds,
            cap=backoff_cap_seconds,
        )
        extra["next_retry_at"] = now + timedelta(seconds=delay) if delay else None
    else:
        extra["next_retry_at"] = None

    try:
        row = transition_task(session, row, target, message, extra=extra)
    except LeaseHeldError:
        return None
    _close_open_attempts(
        session,
        row.id,
        old_token,
        reason=reason,
        failure_class=FailureClass.CRASH,
        status=AttemptStatus.INTERRUPTED,
    )
    journal = Journal(session)
    journal.append(
        row.id,
        EventType.LEASE_EXPIRED,
        reason,
        {"fencing_token": old_token, "to": target.value},
    )
    if target in {TaskStatus.BLOCKED, TaskStatus.WAITING_APPROVAL}:
        journal.append(
            row.id,
            hold.event or EventType.APPROVAL_REQUIRED,
            message,
            {"crash_recovery": True},
        )
    elif target == TaskStatus.RETRY_WAIT:
        journal.append(row.id, EventType.RECOVERED, "expired lease queued for retry", {"attempt": row.attempts})
    else:
        journal.append(row.id, EventType.ERROR, message, {"failure_class": FailureClass.CRASH.value})
    journal.append(row.id, EventType.INTERRUPTED, reason, {"fencing_token": old_token})
    return row


def recover_expired_in_session(
    session: Session,
    *,
    now: datetime,
    backoff_base_seconds: float,
    backoff_cap_seconds: float,
) -> dict[str, str]:
    """Recover every expired in-flight task. Values are the new status."""
    rows = (
        session.query(TaskRow)
        .filter(TaskRow.status.in_([status.value for status in _IN_FLIGHT]))
        .all()
    )
    changed: dict[str, str] = {}
    for row in rows:
        if not lease_expired(row, now):
            continue
        recovered = recover_inflight(
            session,
            row,
            now=now,
            confirm_side_effects=False,
            backoff_base_seconds=backoff_base_seconds,
            backoff_cap_seconds=backoff_cap_seconds,
        )
        if recovered is not None and TaskStatus(recovered.status) not in _IN_FLIGHT:
            changed[recovered.id] = recovered.status
    from max_agent.kernel.dag import wake_satisfied

    wake_satisfied(session, now=now)
    return changed


def claim_in_session(
    session: Session,
    *,
    task_id: str,
    worker_id: str,
    now: datetime,
    ttl_seconds: int,
    req: ScheduleRequest,
    automatic: bool,
    backoff_base_seconds: float,
    backoff_cap_seconds: float,
    max_concurrency: int = 1,
) -> AttemptClaim | TaskRead:
    """Commit-ready claim. Caller must commit before worker.run()."""
    from max_agent.kernel.dag import deps_satisfied

    row = _load(session, task_id, skip_locked=True)
    status = TaskStatus(row.status)
    if status in _TERMINAL_SCHEDULE:
        raise ValueError(f"cannot schedule terminal task status={status}")

    recovered_now = False
    if status in _IN_FLIGHT:
        if not lease_expired(row, now):
            raise LeaseHeldError(
                f"task {task_id} lease held by {row.leased_by} until {row.lease_expires_at}"
            )
        recovered = recover_inflight(
            session,
            row,
            now=now,
            confirm_side_effects=req.confirm_side_effects,
            backoff_base_seconds=backoff_base_seconds,
            backoff_cap_seconds=backoff_cap_seconds,
        )
        if recovered is None:
            raise LeaseHeldError(f"lost recovery race for task {task_id}")
        row = recovered
        recovered_now = True
        status = TaskStatus(row.status)
        if status in _TERMINAL_SCHEDULE:
            raise ValueError(f"cannot schedule terminal task status={status}")

    if automatic and status in {TaskStatus.READY, TaskStatus.RETRY_WAIT}:
        nxt = as_utc(row.next_retry_at)
        if nxt is not None and nxt > as_utc(now):  # type: ignore[operator]
            return task_to_read(row)

    journal = Journal(session)
    if not deps_satisfied(session, row):
        if TaskStatus(row.status) != TaskStatus.BLOCKED:
            row = transition_task(
                session,
                row,
                TaskStatus.BLOCKED,
                "dependencies not satisfied",
                journal=journal,
            )
        return task_to_read(row)

    is_retry = int(row.attempts) > 0 or status == TaskStatus.RETRY_WAIT
    hold = approval_hold(
        tags=list(row.tags or []),
        has_side_effects=bool(row.has_side_effects),
        is_retry=is_retry,
        confirm_side_effects=req.confirm_side_effects,
        allow_auto_retry_side_effects=bool(row.allow_auto_retry_side_effects),
        idempotency_key=row.idempotency_key,
        approved_at=row.approved_at,
        approval_decision=row.approval_decision,
        approval_expires_at=row.approval_expires_at,
        approval_scope=list(row.approval_scope or []),
        now=now,
    )
    if hold.status is not None:
        extra: dict[str, Any] = {
            "last_error": hold.reason,
            "leased_by": None,
            "lease_expires_at": None,
        }
        if hold.decision:
            extra["approval_decision"] = hold.decision
        row = transition_task(session, row, hold.status, hold.reason, extra=extra, journal=journal)
        if hold.event is not None:
            journal.append(
                task_id,
                hold.event,
                hold.reason,
                {"confirm_side_effects": req.confirm_side_effects},
            )
        return task_to_read(row)

    assessment = assess_budget(session, row)
    if assessment.status == "exceeded":
        reason = assessment.reason[:2000] or "budget ceiling exceeded"
        if not cas_update(session, row, {"last_error": reason}):
            raise LeaseHeldError(f"concurrent update on task {task_id}")
        journal.append(task_id, EventType.BUDGET_EXCEEDED, reason, assessment.payload())
        return task_to_read(row)

    if active_inflight_count(session, now) >= max(1, int(max_concurrency)):
        if recovered_now:
            return task_to_read(row)
        raise ConcurrencyLimitError(f"concurrency limit {max_concurrency} reached")

    status = TaskStatus(row.status)
    if status in {TaskStatus.PENDING, TaskStatus.RETRY_WAIT, TaskStatus.BLOCKED, TaskStatus.WAITING_APPROVAL}:
        row = transition_task(
            session,
            row,
            TaskStatus.READY,
            f"approved for run from {status.value}",
            journal=journal,
        )

    kind = req.worker_kind.value if req.worker_kind else row.worker_kind
    expires = now + timedelta(seconds=ttl_seconds)
    new_fence = int(row.fencing_token or 0) + 1
    new_attempts = int(row.attempts) + 1
    row = transition_task(session, row, TaskStatus.LEASED, f"leased by {worker_id}", journal=journal)
    row = transition_task(
        session,
        row,
        TaskStatus.RUNNING,
        "worker started",
        extra={
            "leased_by": worker_id,
            "lease_expires_at": expires,
            "fencing_token": new_fence,
            "attempts": new_attempts,
            "worker_kind": kind,
            "failure_class": None,
            "next_retry_at": None,
        },
        journal=journal,
    )
    attempt_id = str(uuid.uuid4())
    attempt = TaskAttemptRow(
        id=attempt_id,
        task_id=task_id,
        attempt_no=new_attempts,
        status=AttemptStatus.RUNNING.value,
        failure_class=None,
        lease_owner=worker_id,
        fencing_token=new_fence,
        lease_expires_at=expires,
        started_at=now,
        finished_at=None,
        idempotency_key=row.idempotency_key,
        worker_kind=kind,
        cost_source="unknown",
        cost_marker="unknown",
    )
    session.add(attempt)
    session.flush()
    journal.append(
        task_id,
        EventType.LEASED,
        f"leased by {worker_id}",
        {"worker_kind": kind, "fencing_token": new_fence, "lease_expires_at": expires.isoformat()},
    )
    journal.append(task_id, EventType.SCHEDULED, "schedule accepted", {"worker_kind": kind})
    journal.append(
        task_id,
        EventType.ATTEMPT_STARTED,
        f"attempt {new_attempts} started",
        {"attempt": new_attempts, "attempt_id": attempt_id, "fencing_token": new_fence, "worker_kind": kind},
    )
    return AttemptClaim(
        task_id=task_id,
        attempt_id=attempt_id,
        attempt_no=new_attempts,
        fencing_token=new_fence,
        lease_owner=worker_id,
        lease_expires_at=expires,
        goal=row.goal,
        acceptance_criteria=tuple(row.acceptance_criteria or []),
        payload=dict(row.worker_payload or {}),
        timeout_seconds=int(row.timeout_seconds),
        worker_kind=kind,
        has_side_effects=bool(row.has_side_effects),
    )
