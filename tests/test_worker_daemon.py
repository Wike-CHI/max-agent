"""Integration coverage for the long-running worker daemon."""

from __future__ import annotations

import threading
from datetime import timedelta

from max_agent.db.models import TaskAttemptRow, TaskRow, utcnow
from max_agent.db.session import get_session_factory, session_scope
from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.models.enums import TaskStatus, WorkerKind
from max_agent.models.schemas import TaskCreate, WorkerResult
from max_agent.workers.base import Worker, WorkerContext
from max_agent.workers.daemon import WorkerDaemon


def _create(db_url: str, goal: str) -> str:
    with session_scope(db_url) as session:
        return TaskService(session).create(
            TaskCreate(
                goal=goal,
                acceptance_criteria=["worker_ok"],
                worker_kind=WorkerKind.MOCK,
                worker_payload={"mode": "success"},
            )
        ).id


def _status(db_url: str, task_id: str) -> TaskStatus:
    with session_scope(db_url) as session:
        row = session.get(TaskRow, task_id)
        assert row is not None
        return TaskStatus(row.status)


class _SuccessWorker(Worker):
    kind = WorkerKind.MOCK

    def __init__(self) -> None:
        self.calls = 0
        self.finished = threading.Event()

    def capabilities(self) -> set[str]:
        return {"mock"}

    def run(self, ctx: WorkerContext) -> WorkerResult:
        self.calls += 1
        self.finished.set()
        return WorkerResult(ok=True, summary="ok", outputs={"answer": "ok"})


class _BlockWorker(_SuccessWorker):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def run(self, ctx: WorkerContext) -> WorkerResult:
        self.entered.set()
        assert self.release.wait(5)
        return super().run(ctx)


def _daemon(db_url: str, worker_id: str, worker: Worker) -> WorkerDaemon:
    scheduler = Scheduler(
        session_factory=get_session_factory(db_url),
        worker_id=worker_id,
        worker_resolver=lambda _kind: worker,
        heartbeat_interval_seconds=0.05,
        lease_ttl_seconds=60,
        backoff_base_seconds=0,
    )
    return WorkerDaemon(scheduler, poll_interval_seconds=0.01, max_poll_interval_seconds=0.02)


def _run_in_thread(daemon: WorkerDaemon) -> threading.Thread:
    thread = threading.Thread(target=daemon.run)
    thread.start()
    return thread


def test_daemon_claims_and_finishes_ready_task(db_url: str):
    task_id = _create(db_url, "automatic pickup")
    worker = _SuccessWorker()
    daemon = _daemon(db_url, "daemon-a", worker)
    thread = _run_in_thread(daemon)
    try:
        assert worker.finished.wait(5)
    finally:
        daemon.request_stop()
        thread.join(5)
    assert not thread.is_alive()
    assert worker.calls == 1
    assert _status(db_url, task_id) == TaskStatus.SUCCEEDED


def test_daemon_reclaims_expired_lease_from_killed_worker(db_url: str):
    task_id = _create(db_url, "reclaim after kill")
    Scheduler(session_factory=get_session_factory(db_url), worker_id="killed").acquire_lease(task_id)
    with session_scope(db_url) as session:
        row = session.get(TaskRow, task_id)
        assert row is not None
        row.lease_expires_at = utcnow() - timedelta(seconds=1)

    worker = _SuccessWorker()
    daemon = _daemon(db_url, "daemon-b", worker)
    thread = _run_in_thread(daemon)
    try:
        assert worker.finished.wait(5)
    finally:
        daemon.request_stop()
        thread.join(5)
    assert not thread.is_alive()
    assert _status(db_url, task_id) == TaskStatus.SUCCEEDED
    with session_scope(db_url) as session:
        attempts = (
            session.query(TaskAttemptRow)
            .filter(TaskAttemptRow.task_id == task_id)
            .order_by(TaskAttemptRow.attempt_no.asc())
            .all()
        )
        assert [attempt.status for attempt in attempts] == ["interrupted", "succeeded"]
        assert attempts[1].lease_owner == "daemon-b"


def test_graceful_shutdown_finishes_active_task_without_claiming_another(db_url: str):
    active_id = _create(db_url, "finish active")
    worker = _BlockWorker()
    daemon = _daemon(db_url, "daemon-stop", worker)
    thread = _run_in_thread(daemon)
    try:
        assert worker.entered.wait(5)
        pending_id = _create(db_url, "leave pending")
        daemon.request_stop()
    finally:
        worker.release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert _status(db_url, active_id) == TaskStatus.SUCCEEDED
    assert _status(db_url, pending_id) == TaskStatus.READY
    with session_scope(db_url) as session:
        pending = session.get(TaskRow, pending_id)
        assert pending is not None
        assert pending.attempts == 0
