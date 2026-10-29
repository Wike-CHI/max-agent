"""Run a claimed attempt outside any SQLAlchemy session, then finalize."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy.orm import Session, sessionmaker

from max_agent.db.models import TaskAttemptRow, TaskRow, utcnow
from max_agent.kernel.errors import StaleFenceError
from max_agent.kernel.journal import Journal
from max_agent.kernel.lease import AttemptClaim, select_task_for_finalize
from max_agent.kernel.mutate import transition_task
from max_agent.kernel.readmodel import task_to_read
from max_agent.kernel.retry import backoff_delay_seconds
from max_agent.kernel.timeutil import as_utc
from max_agent.kernel.verification import verify
from max_agent.models.enums import AttemptStatus, EventType, FailureClass, TaskStatus, WorkerKind
from max_agent.models.schemas import TaskRead, WorkerResult
from max_agent.workers.base import Worker, WorkerContext
from max_agent.workers.registry import get_worker

_IN_FLIGHT = frozenset({TaskStatus.LEASED, TaskStatus.RUNNING, TaskStatus.VERIFYING})


def next_ready_id(session: Session, now: datetime, exclude: set[str]) -> str | None:
    rows = (
        session.query(TaskRow)
        .filter(TaskRow.status.in_([TaskStatus.READY.value, TaskStatus.RETRY_WAIT.value]))
        .order_by(TaskRow.priority.asc(), TaskRow.created_at.asc())
        .all()
    )
    now_utc = as_utc(now)
    for row in rows:
        if row.id in exclude:
            continue
        nxt = as_utc(row.next_retry_at)
        if nxt is not None and now_utc is not None and nxt > now_utc:
            continue
        return row.id
    return None


def execute_claim(
    claim: AttemptClaim,
    *,
    session_factory: sessionmaker[Session],
    worker_resolver: Callable[[WorkerKind], Worker] | None,
) -> tuple[WorkerResult, FailureClass | None]:
    """Run the worker. The session factory is used only for short tool-call commits."""
    worker = _resolve_worker(claim.worker_kind, worker_resolver)
    tool_calls: list[dict[str, Any]] = []

    def on_tool(name: str, args: dict[str, Any]) -> None:
        tool_calls.append({"tool": name, "args": args})
        _journal_tool(session_factory, claim, name, args)

    ctx = WorkerContext(
        task_id=claim.task_id,
        goal=claim.goal,
        acceptance_criteria=list(claim.acceptance_criteria),
        payload=dict(claim.payload),
        timeout_seconds=claim.timeout_seconds,
        on_tool_call=on_tool,
    )
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["result"] = worker.run(ctx)
        except BaseException as exc:  # noqa: BLE001 — re-raised or stored for finalize
            box["error"] = exc

    thread = threading.Thread(target=_target, name=f"max-agent-{claim.task_id[:8]}", daemon=True)
    thread.start()
    thread.join(claim.timeout_seconds)
    if thread.is_alive():
        # The thread cannot be killed. Fencing stops it from finalizing; a
        # side-effecting worker may still be running in-process.
        return (
            WorkerResult(
                ok=False,
                summary="worker timed out",
                outputs={},
                tool_calls=tool_calls,
                recoverable=True,
                error=f"timeout after {claim.timeout_seconds}s",
            ),
            FailureClass.TIMEOUT,
        )
    if "error" in box:
        err = box["error"]
        if isinstance(err, (KeyboardInterrupt, SystemExit)):
            raise err
        return (
            WorkerResult(
                ok=False,
                summary="worker raised exception",
                outputs={},
                tool_calls=tool_calls,
                recoverable=True,
                error=str(err),
            ),
            FailureClass.EXECUTION,
        )
    result = box.get("result")
    if not isinstance(result, WorkerResult):
        return (
            WorkerResult(
                ok=False,
                summary="worker returned no result",
                outputs={},
                tool_calls=tool_calls,
                recoverable=True,
                error="worker returned no result",
            ),
            FailureClass.EXECUTION,
        )
    if tool_calls and not result.tool_calls:
        result = result.model_copy(update={"tool_calls": tool_calls})
    return result, None


def finalize_in_session(
    session: Session,
    claim: AttemptClaim,
    result: WorkerResult,
    failure_hint: FailureClass | None,
    *,
    now: datetime,
    backoff_base_seconds: float,
    backoff_cap_seconds: float,
) -> TaskRead:
    row = _load_for_finalize(session, claim)
    if int(row.fencing_token or 0) != claim.fencing_token or row.leased_by != claim.lease_owner:
        _reject_fence(session, row, claim)
    if TaskStatus(row.status) not in _IN_FLIGHT:
        _reject_fence(session, row, claim)

    journal = Journal(session)
    journal.append(
        claim.task_id,
        EventType.WORKER_OUTPUT,
        "worker outputs",
        {"outputs": result.outputs, "tool_calls": result.tool_calls, "attempt": claim.attempt_no},
    )
    journal.append(
        claim.task_id,
        EventType.ATTEMPT_FINISHED,
        result.summary,
        {
            "attempt": claim.attempt_no,
            "ok": result.ok,
            "error": result.error,
            "recoverable": result.recoverable,
            "failure_class": failure_hint.value if failure_hint else None,
        },
    )

    if failure_hint == FailureClass.TIMEOUT or not result.ok:
        failure_class = failure_hint or FailureClass.EXECUTION
        attempt_status = AttemptStatus.TIMED_OUT if failure_class == FailureClass.TIMEOUT else AttemptStatus.FAILED
        _finish_attempt(
            session,
            claim,
            status=attempt_status,
            failure_class=failure_class,
            worker_ok=bool(result.ok),
            recoverable=bool(result.recoverable) if failure_class != FailureClass.TIMEOUT else True,
            error=result.error or result.summary,
            summary=result.summary,
        )
        row = _fail_task(
            session,
            row,
            err=result.error or result.summary,
            summary=result.summary,
            failure_class=failure_class,
            recoverable=True if failure_class == FailureClass.TIMEOUT else bool(result.recoverable),
            now=now,
            backoff_base_seconds=backoff_base_seconds,
            backoff_cap_seconds=backoff_cap_seconds,
            journal=journal,
        )
        return task_to_read(row)

    row = transition_task(session, row, TaskStatus.VERIFYING, "verifying acceptance criteria", journal=journal)
    verdict = verify(list(row.acceptance_criteria or []), result, payload=dict(row.worker_payload or {}))
    failure_class = None if verdict.passed else FailureClass.VERIFICATION
    journal.append(
        claim.task_id,
        EventType.VERIFICATION,
        verdict.message,
        {
            "passed": verdict.passed,
            "checks": verdict.checks,
            "failure_class": failure_class.value if failure_class else None,
        },
    )
    if verdict.passed:
        _finish_attempt(
            session,
            claim,
            status=AttemptStatus.SUCCEEDED,
            failure_class=None,
            worker_ok=True,
            recoverable=True,
            error=None,
            summary=result.summary,
        )
        row = transition_task(
            session,
            row,
            TaskStatus.SUCCEEDED,
            "accepted",
            extra={
                "result_summary": result.summary,
                "last_error": None,
                "leased_by": None,
                "lease_expires_at": None,
                "failure_class": None,
                "next_retry_at": None,
            },
            journal=journal,
        )
        return task_to_read(row)

    _finish_attempt(
        session,
        claim,
        status=AttemptStatus.FAILED,
        failure_class=FailureClass.VERIFICATION,
        worker_ok=True,
        recoverable=True,
        error=verdict.message,
        summary=result.summary,
    )
    row = _fail_task(
        session,
        row,
        err=verdict.message,
        summary=result.summary,
        failure_class=FailureClass.VERIFICATION,
        recoverable=True,
        now=now,
        backoff_base_seconds=backoff_base_seconds,
        backoff_cap_seconds=backoff_cap_seconds,
        journal=journal,
    )
    return task_to_read(row)


def _resolve_worker(kind: str, resolver: Callable[[WorkerKind], Worker] | None) -> Worker:
    parsed = WorkerKind(kind)
    if resolver is not None:
        return resolver(parsed)
    return get_worker(parsed)


def _journal_tool(
    session_factory: sessionmaker[Session],
    claim: AttemptClaim,
    name: str,
    args: dict[str, Any],
) -> None:
    session = session_factory()
    try:
        row = session.get(TaskRow, claim.task_id)
        if row is None or int(row.fencing_token or 0) != claim.fencing_token:
            session.rollback()
            return
        Journal(session).append(
            claim.task_id,
            EventType.TOOL_CALL,
            name,
            {"args": args, "attempt": claim.attempt_no, "fencing_token": claim.fencing_token},
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _load_for_finalize(session: Session, claim: AttemptClaim) -> TaskRow:
    dialect = session.get_bind().dialect.name
    stmt = select_task_for_finalize(dialect, claim.task_id)
    row = session.execute(stmt).scalar_one_or_none()
    if row is not None:
        return row
    if session.get(TaskRow, claim.task_id) is None:
        raise KeyError(f"task not found: {claim.task_id}")
    raise StaleFenceError(f"could not lock task {claim.task_id} to finalize fence {claim.fencing_token}")


def _reject_fence(session: Session, row: TaskRow, claim: AttemptClaim) -> None:
    Journal(session).append(
        row.id,
        EventType.FENCING_REJECTED,
        "stale fencing token ignored",
        {
            "claim_fencing_token": claim.fencing_token,
            "current_fencing_token": int(row.fencing_token or 0),
            "claim_owner": claim.lease_owner,
            "leased_by": row.leased_by,
        },
    )
    raise StaleFenceError(
        f"stale fence {claim.fencing_token} for task {claim.task_id}; current token is {row.fencing_token}"
    )


def _finish_attempt(
    session: Session,
    claim: AttemptClaim,
    *,
    status: AttemptStatus,
    failure_class: FailureClass | None,
    worker_ok: bool,
    recoverable: bool,
    error: str | None,
    summary: str,
) -> None:
    attempt = session.get(TaskAttemptRow, claim.attempt_id)
    if attempt is None or int(attempt.fencing_token) != claim.fencing_token:
        raise StaleFenceError(f"attempt {claim.attempt_id} does not match fence {claim.fencing_token}")
    attempt.status = status.value
    attempt.failure_class = failure_class.value if failure_class else None
    attempt.worker_ok = worker_ok
    attempt.recoverable = recoverable
    attempt.error = error
    attempt.summary = summary
    attempt.finished_at = utcnow()
    session.flush()


def _fail_task(
    session: Session,
    row: TaskRow,
    *,
    err: str,
    summary: str,
    failure_class: FailureClass,
    recoverable: bool,
    now: datetime,
    backoff_base_seconds: float,
    backoff_cap_seconds: float,
    journal: Journal,
) -> TaskRow:
    exhausted = (not recoverable) or int(row.attempts) >= int(row.max_attempts)
    target = TaskStatus.FAILED if exhausted else TaskStatus.RETRY_WAIT
    extra: dict[str, Any] = {
        "last_error": err,
        "result_summary": summary,
        "leased_by": None,
        "lease_expires_at": None,
        "failure_class": failure_class.value,
    }
    if target == TaskStatus.RETRY_WAIT:
        delay = backoff_delay_seconds(int(row.attempts), base=backoff_base_seconds, cap=backoff_cap_seconds)
        extra["next_retry_at"] = now + timedelta(seconds=delay) if delay else None
    else:
        extra["next_retry_at"] = None
    message = f"failed after {row.attempts} attempt(s): {err}" if target == TaskStatus.FAILED else f"recoverable failure: {err}"
    row = transition_task(session, row, target, message, extra=extra, journal=journal)
    journal.append(
        row.id,
        EventType.ERROR,
        message,
        {
            "attempts": row.attempts,
            "max_attempts": row.max_attempts,
            "failure_class": failure_class.value,
            "error": err,
        },
    )
    return row
