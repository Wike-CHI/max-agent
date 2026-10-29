"""Integration coverage for atomic leases, fencing, and crash recovery."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy.dialects import postgresql, sqlite

from max_agent.db.models import TaskAttemptRow, TaskRow, utcnow
from max_agent.db.session import get_session_factory, session_scope
from max_agent.kernel.errors import LeaseHeldError, StaleFenceError
from max_agent.kernel.lease import select_task_for_claim
from max_agent.kernel.retry import backoff_delay_seconds
from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.models.enums import FailureClass, TaskStatus, WorkerKind
from max_agent.models.schemas import BudgetConstraints, ScheduleRequest, TaskCreate, WorkerResult
from max_agent.workers.base import Worker, WorkerContext


def _factory(db_url: str):
    return get_session_factory(db_url)


def _create(db_url: str, **kwargs) -> str:
    data = TaskCreate(**kwargs)
    with session_scope(db_url) as session:
        return TaskService(session).create(data).id


def _attempts(db_url: str, task_id: str) -> list[TaskAttemptRow]:
    with session_scope(db_url) as session:
        return (
            session.query(TaskAttemptRow)
            .filter(TaskAttemptRow.task_id == task_id)
            .order_by(TaskAttemptRow.attempt_no.asc())
            .all()
        )


class _SuccessWorker(Worker):
    kind = WorkerKind.MOCK

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self) -> set[str]:
        return {"mock"}

    def run(self, ctx: WorkerContext) -> WorkerResult:
        self.calls += 1
        return WorkerResult(ok=True, summary="ok", outputs={"answer": "ok"})


class _BlockWorker(Worker):
    """Holds execution until released so a second scheduler can race the lease."""

    kind = WorkerKind.MOCK

    def __init__(self) -> None:
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def capabilities(self) -> set[str]:
        return {"mock"}

    def run(self, ctx: WorkerContext) -> WorkerResult:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(5)
        return WorkerResult(ok=True, summary="ok", outputs={"answer": "ok"})


def test_backoff_delay_is_exponential_and_capped():
    assert backoff_delay_seconds(1, base=2.0, cap=10.0) == 2.0
    assert backoff_delay_seconds(2, base=2.0, cap=10.0) == 4.0
    assert backoff_delay_seconds(3, base=2.0, cap=10.0) == 8.0
    assert backoff_delay_seconds(4, base=2.0, cap=10.0) == 10.0
    assert backoff_delay_seconds(2, base=0.0, cap=10.0) == 0.0


def test_postgresql_claim_uses_skip_locked_sqlite_does_not():
    pg = str(
        select_task_for_claim("postgresql", "task-1").compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    lite = str(
        select_task_for_claim("sqlite", "task-1").compile(
            dialect=sqlite.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "FOR UPDATE SKIP LOCKED" in pg.upper()
    assert "FOR UPDATE" not in lite.upper()


def test_duplicate_schedule_only_one_worker_runs(db_url: str):
    task_id = _create(
        db_url,
        goal="once",
        acceptance_criteria=["worker_ok"],
        worker_kind=WorkerKind.MOCK,
        worker_payload={"mode": "success"},
    )
    factory = _factory(db_url)
    worker = _BlockWorker()
    errors: list[BaseException] = []

    def runner(worker_id: str) -> None:
        try:
            Scheduler(
                session_factory=factory,
                worker_id=worker_id,
                worker_resolver=lambda _kind: worker,
            ).run_one(task_id)
        except (LeaseHeldError, ValueError) as exc:
            errors.append(exc)

    first = threading.Thread(target=runner, args=("worker-a",))
    second = threading.Thread(target=runner, args=("worker-b",))
    first.start()
    second.start()
    assert worker.entered.wait(5)
    # The loser must observe the committed lease while the winner is still inside run().
    assert worker.calls == 1
    worker.release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert worker.calls == 1
    assert len(errors) == 1
    with session_scope(db_url) as session:
        row = session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == TaskStatus.SUCCEEDED.value
        assert row.fencing_token == 1


def test_expired_lease_reclaim_rejects_stale_finalize(db_url: str):
    task_id = _create(
        db_url,
        goal="reclaim",
        acceptance_criteria=["worker_ok"],
        worker_kind=WorkerKind.MOCK,
        worker_payload={"mode": "success", "answer": "ok"},
    )
    factory = _factory(db_url)
    owner = Scheduler(session_factory=factory, worker_id="owner")
    claim = owner.acquire_lease(task_id)
    assert claim.fencing_token == 1
    assert claim.lease_owner == "owner"

    with session_scope(db_url) as session:
        row = session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == TaskStatus.RUNNING.value
        assert row.leased_by == "owner"
        row.lease_expires_at = utcnow() - timedelta(seconds=30)

    winner = _SuccessWorker()
    out = Scheduler(
        session_factory=factory,
        worker_id="reclaimer",
        worker_resolver=lambda _kind: winner,
        backoff_base_seconds=0,
    ).run_one(task_id)
    assert winner.calls == 1
    assert out.status == TaskStatus.SUCCEEDED
    assert out.attempts == 2

    with pytest.raises(StaleFenceError):
        owner.finalize_lease(
            claim,
            WorkerResult(ok=True, summary="late owner", outputs={"answer": "stale"}),
        )

    with session_scope(db_url) as session:
        row = session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == TaskStatus.SUCCEEDED.value
        assert row.result_summary != "late owner"
        attempts = (
            session.query(TaskAttemptRow)
            .filter(TaskAttemptRow.task_id == task_id)
            .order_by(TaskAttemptRow.attempt_no.asc())
            .all()
        )
        assert [a.fencing_token for a in attempts] == [1, 2]
        assert attempts[0].status == "interrupted"
        assert attempts[0].failure_class == FailureClass.CRASH.value
        assert attempts[1].status == "succeeded"
        assert attempts[1].lease_owner == "reclaimer"


def test_lost_worker_reclaim_is_single_winner(db_url: str):
    task_id = _create(
        db_url,
        goal="lost",
        acceptance_criteria=["worker_ok"],
        worker_kind=WorkerKind.MOCK,
        worker_payload={"mode": "success"},
        budget=BudgetConstraints(max_attempts=4),
    )
    factory = _factory(db_url)
    Scheduler(session_factory=factory, worker_id="lost").acquire_lease(task_id)
    with session_scope(db_url) as session:
        row = session.get(TaskRow, task_id)
        assert row is not None
        row.lease_expires_at = utcnow() - timedelta(seconds=30)

    worker = _BlockWorker()
    errors: list[BaseException] = []

    def runner(worker_id: str) -> None:
        try:
            Scheduler(
                session_factory=factory,
                worker_id=worker_id,
                worker_resolver=lambda _kind: worker,
                backoff_base_seconds=0,
            ).run_one(task_id)
        except (LeaseHeldError, ValueError) as exc:
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=runner, args=(f"w{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    assert worker.entered.wait(5)
    assert worker.calls == 1
    worker.release.set()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert worker.calls == 1
    assert len(errors) == 1
    attempts = _attempts(db_url, task_id)
    assert len(attempts) == 2
    assert attempts[0].failure_class == FailureClass.CRASH.value
    assert len({a.fencing_token for a in attempts}) == 2


def test_kill_style_interruption_recovers_without_finalize(db_url: str):
    """Claim is committed before run(); a kill skips finalize and stays recoverable."""

    class KillAfterVisibleLease(Worker):
        kind = WorkerKind.MOCK

        def capabilities(self) -> set[str]:
            return set()

        def run(self, ctx: WorkerContext) -> WorkerResult:
            with session_scope(db_url) as session:
                row = session.get(TaskRow, ctx.task_id)
                assert row is not None
                assert row.status == TaskStatus.RUNNING.value
                assert row.leased_by == "victim"
                assert row.lease_expires_at is not None
                assert row.fencing_token == 1
                count = session.query(TaskAttemptRow).filter_by(task_id=ctx.task_id).count()
                assert count == 1
            raise KeyboardInterrupt()

    task_id = _create(
        db_url,
        goal="kill",
        acceptance_criteria=["worker_ok"],
        worker_kind=WorkerKind.MOCK,
        worker_payload={"mode": "success"},
        budget=BudgetConstraints(max_attempts=3),
    )
    factory = _factory(db_url)
    with pytest.raises(KeyboardInterrupt):
        Scheduler(
            session_factory=factory,
            worker_id="victim",
            worker_resolver=lambda _kind: KillAfterVisibleLease(),
        ).run_one(task_id)

    with session_scope(db_url) as session:
        row = session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == TaskStatus.RUNNING.value
        row.lease_expires_at = utcnow() - timedelta(seconds=30)

    follower = _SuccessWorker()
    out = Scheduler(
        session_factory=factory,
        worker_id="restart",
        worker_resolver=lambda _kind: follower,
        backoff_base_seconds=0,
    ).run_one(task_id)
    assert follower.calls == 1
    assert out.status == TaskStatus.SUCCEEDED
    assert out.attempts == 2


def test_non_idempotent_side_effect_is_not_auto_replayed(db_url: str, tmp_path):
    marker = tmp_path / "side-effect.txt"

    class EffectThenKill(Worker):
        kind = WorkerKind.MOCK
        calls = 0

        def capabilities(self) -> set[str]:
            return set()

        def run(self, ctx: WorkerContext) -> WorkerResult:
            type(self).calls += 1
            marker.write_text("charged", encoding="utf-8")
            raise KeyboardInterrupt()

    task_id = _create(
        db_url,
        goal="charge",
        acceptance_criteria=["worker_ok"],
        worker_kind=WorkerKind.MOCK,
        worker_payload={"mode": "success"},
        has_side_effects=True,
        budget=BudgetConstraints(max_attempts=3, allow_auto_retry_side_effects=False),
    )
    factory = _factory(db_url)
    with pytest.raises(KeyboardInterrupt):
        Scheduler(
            session_factory=factory,
            worker_id="victim",
            worker_resolver=lambda _kind: EffectThenKill(),
        ).run_one(task_id)
    assert marker.read_text(encoding="utf-8") == "charged"
    assert EffectThenKill.calls == 1

    with session_scope(db_url) as session:
        row = session.get(TaskRow, task_id)
        assert row is not None
        row.lease_expires_at = utcnow() - timedelta(seconds=30)

    replay = _SuccessWorker()
    blocked = Scheduler(
        session_factory=factory,
        worker_id="restart",
        worker_resolver=lambda _kind: replay,
        backoff_base_seconds=0,
    ).run_one(task_id)
    assert replay.calls == 0
    assert blocked.status == TaskStatus.BLOCKED
    assert "side-effect" in (blocked.last_error or "")

    confirmed = Scheduler(
        session_factory=factory,
        worker_id="restart",
        worker_resolver=lambda _kind: replay,
        backoff_base_seconds=0,
    ).run_one(task_id, ScheduleRequest(confirm_side_effects=True))
    assert replay.calls == 1
    assert confirmed.status == TaskStatus.SUCCEEDED


def test_idempotency_key_allows_side_effect_replay_after_crash(db_url: str):
    task_id = _create(
        db_url,
        goal="idem",
        acceptance_criteria=["worker_ok"],
        worker_kind=WorkerKind.MOCK,
        worker_payload={"mode": "success"},
        has_side_effects=True,
        idempotency_key="pay-42",
        budget=BudgetConstraints(max_attempts=3),
    )
    factory = _factory(db_url)
    Scheduler(session_factory=factory, worker_id="lost").acquire_lease(task_id)
    with session_scope(db_url) as session:
        row = session.get(TaskRow, task_id)
        assert row is not None
        row.lease_expires_at = utcnow() - timedelta(seconds=5)

    replay = _SuccessWorker()
    out = Scheduler(
        session_factory=factory,
        worker_id="restart",
        worker_resolver=lambda _kind: replay,
        backoff_base_seconds=0,
    ).run_one(task_id)
    assert replay.calls == 1
    assert out.status == TaskStatus.SUCCEEDED


def test_verification_failure_is_not_execution_failure(db_url: str):
    task_id = _create(
        db_url,
        goal="wrong output",
        acceptance_criteria=["output_equals:answer=expected"],
        worker_kind=WorkerKind.MOCK,
        worker_payload={"mode": "fail_acceptance"},
        budget=BudgetConstraints(max_attempts=1),
    )
    out = Scheduler(session_factory=_factory(db_url)).run_one(task_id)
    assert out.status == TaskStatus.FAILED
    assert out.failure_class == FailureClass.VERIFICATION.value
    attempts = _attempts(db_url, task_id)
    assert len(attempts) == 1
    assert attempts[0].worker_ok is True
    assert attempts[0].failure_class == FailureClass.VERIFICATION.value


def test_execution_failure_classification(db_url: str):
    task_id = _create(
        db_url,
        goal="hard",
        acceptance_criteria=["worker_ok"],
        worker_kind=WorkerKind.MOCK,
        worker_payload={"mode": "fail_hard"},
        budget=BudgetConstraints(max_attempts=5),
    )
    out = Scheduler(session_factory=_factory(db_url)).run_one(task_id)
    assert out.status == TaskStatus.FAILED
    assert out.failure_class == FailureClass.EXECUTION.value
    attempts = _attempts(db_url, task_id)
    assert attempts[0].worker_ok is False
    assert attempts[0].failure_class == FailureClass.EXECUTION.value


def test_timeout_is_reentered_as_timeout(db_url: str):
    task_id = _create(
        db_url,
        goal="slow",
        acceptance_criteria=["worker_ok"],
        worker_kind=WorkerKind.MOCK,
        worker_payload={"mode": "sleep", "delay_seconds": 30},
        timeout_seconds=1,
        budget=BudgetConstraints(max_attempts=3, timeout_seconds=1),
    )
    out = Scheduler(session_factory=_factory(db_url), backoff_base_seconds=0).run_one(task_id)
    assert out.status == TaskStatus.RETRY_WAIT
    assert out.failure_class == FailureClass.TIMEOUT.value
    attempts = _attempts(db_url, task_id)
    assert attempts[0].status == "timed_out"
    assert attempts[0].failure_class == FailureClass.TIMEOUT.value


def test_automatic_pickup_waits_for_backoff(db_url: str):
    task_id = _create(
        db_url,
        goal="flaky",
        acceptance_criteria=["worker_ok"],
        worker_kind=WorkerKind.MOCK,
        worker_payload={"mode": "fail_recoverable"},
        budget=BudgetConstraints(max_attempts=3),
    )
    factory = _factory(db_url)
    scheduler = Scheduler(session_factory=factory, backoff_base_seconds=60, backoff_cap_seconds=60)
    out = scheduler.run_one(task_id)
    assert out.status == TaskStatus.RETRY_WAIT
    assert out.next_retry_at is not None
    with pytest.raises(LookupError):
        scheduler.run_one(None)


def test_crash_at_max_attempts_does_not_run_again(db_url: str):
    task_id = _create(
        db_url,
        goal="once only",
        acceptance_criteria=["worker_ok"],
        worker_kind=WorkerKind.MOCK,
        worker_payload={"mode": "success"},
        budget=BudgetConstraints(max_attempts=1),
    )
    factory = _factory(db_url)
    Scheduler(session_factory=factory, worker_id="lost").acquire_lease(task_id)
    with session_scope(db_url) as session:
        row = session.get(TaskRow, task_id)
        assert row is not None
        row.lease_expires_at = utcnow() - timedelta(seconds=5)

    follower = _SuccessWorker()
    out = Scheduler(
        session_factory=factory,
        worker_id="restart",
        worker_resolver=lambda _kind: follower,
    ).run_one(task_id)
    assert follower.calls == 0
    assert out.status == TaskStatus.FAILED
    assert out.failure_class == FailureClass.CRASH.value


def test_alembic_upgrade_adds_attempts_and_fence(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("MAX_AGENT_DATABASE_URL", url)
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    inspector = inspect(engine)
    assert "task_attempts" in inspector.get_table_names()
    task_cols = {col["name"] for col in inspector.get_columns("tasks")}
    assert {"fencing_token", "next_retry_at", "failure_class"} <= task_cols
    attempt_cols = {col["name"] for col in inspector.get_columns("task_attempts")}
    assert {"lease_owner", "lease_expires_at", "fencing_token"} <= attempt_cols
    engine.dispose()
