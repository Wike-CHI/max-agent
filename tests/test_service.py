from __future__ import annotations

from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.models.enums import TaskStatus, WorkerKind
from max_agent.models.schemas import BudgetConstraints, ScheduleRequest, TaskCreate


def test_create_persist_and_idempotent(session):
    svc = TaskService(session)
    t1 = svc.create(
        TaskCreate(
            goal="demo",
            acceptance_criteria=["worker_ok"],
            idempotency_key="k1",
            worker_kind=WorkerKind.MOCK,
            worker_payload={"mode": "success", "answer": "ok"},
        )
    )
    assert t1.status == TaskStatus.READY
    t2 = svc.create(
        TaskCreate(
            goal="demo-dup",
            acceptance_criteria=["worker_ok"],
            idempotency_key="k1",
        )
    )
    assert t1.id == t2.id
    events = svc.list_events(t1.id)
    assert any(e.event_type.value == "idempotent_hit" for e in events)


def test_mock_success_and_journal(session):
    svc = TaskService(session)
    t = svc.create(
        TaskCreate(
            goal="say hello",
            acceptance_criteria=["worker_ok", "output_equals:answer=hello"],
            worker_kind=WorkerKind.MOCK,
            worker_payload={"mode": "success", "answer": "hello"},
        )
    )
    out = Scheduler(session).run_one(t.id)
    assert out.status == TaskStatus.SUCCEEDED
    assert out.attempts == 1
    events = svc.list_events(t.id)
    types = [e.event_type.value for e in events]
    assert "created" in types
    assert "attempt_started" in types
    assert "verification" in types
    assert "status_changed" in types


def test_recoverable_failure_then_retry(session):
    svc = TaskService(session)
    t = svc.create(
        TaskCreate(
            goal="flaky",
            acceptance_criteria=["worker_ok"],
            worker_kind=WorkerKind.MOCK,
            worker_payload={"mode": "fail_recoverable"},
            budget=BudgetConstraints(max_attempts=3),
        )
    )
    out = Scheduler(session).run_one(t.id)
    assert out.status == TaskStatus.RETRY_WAIT
    assert out.attempts == 1

    # switch payload to success and retry
    row = svc._get_row(t.id)
    row.worker_payload = {"mode": "success", "answer": "recovered"}
    session.flush()
    out2 = Scheduler(session).run_one(t.id)
    assert out2.status == TaskStatus.SUCCEEDED
    assert out2.attempts == 2


def test_hard_failure(session):
    svc = TaskService(session)
    t = svc.create(
        TaskCreate(
            goal="hard fail",
            acceptance_criteria=["worker_ok"],
            worker_kind=WorkerKind.MOCK,
            worker_payload={"mode": "fail_hard"},
            budget=BudgetConstraints(max_attempts=5),
        )
    )
    out = Scheduler(session).run_one(t.id)
    assert out.status == TaskStatus.FAILED


def test_acceptance_failure_not_success(session):
    svc = TaskService(session)
    t = svc.create(
        TaskCreate(
            goal="bad accept",
            acceptance_criteria=["output_equals:answer=expected"],
            worker_kind=WorkerKind.MOCK,
            worker_payload={"mode": "fail_acceptance"},
            budget=BudgetConstraints(max_attempts=1),
        )
    )
    out = Scheduler(session).run_one(t.id)
    # worker ok but verification fails → failed (max attempts 1)
    assert out.status == TaskStatus.FAILED


def test_side_effects_block_retry_without_confirm(session):
    svc = TaskService(session)
    t = svc.create(
        TaskCreate(
            goal="side effect",
            acceptance_criteria=["worker_ok"],
            worker_kind=WorkerKind.MOCK,
            worker_payload={"mode": "fail_recoverable"},
            has_side_effects=True,
            budget=BudgetConstraints(max_attempts=3),
        )
    )
    out1 = Scheduler(session).run_one(t.id)
    assert out1.status == TaskStatus.RETRY_WAIT
    # retry without confirm → blocked
    out2 = Scheduler(session).run_one(t.id, ScheduleRequest(confirm_side_effects=False))
    assert out2.status == TaskStatus.BLOCKED
    # with confirm, still fail_recoverable → retry_wait again
    row = svc._get_row(t.id)
    row.worker_payload = {"mode": "success"}
    # need to move blocked → can schedule after confirm
    # schedule with confirm should unstick
    out3 = Scheduler(session).run_one(t.id, ScheduleRequest(confirm_side_effects=True))
    assert out3.status == TaskStatus.SUCCEEDED


def test_cancel(session):
    svc = TaskService(session)
    t = svc.create(TaskCreate(goal="cancel me", acceptance_criteria=["worker_ok"]))
    out = svc.cancel(t.id)
    assert out.status == TaskStatus.CANCELLED


def test_persistence_across_sessions(db_url):
    from max_agent.db.session import session_scope

    with session_scope(db_url) as s1:
        t = TaskService(s1).create(
            TaskCreate(
                goal="persist",
                acceptance_criteria=["worker_ok"],
                worker_payload={"mode": "success"},
            )
        )
        tid = t.id
        Scheduler(s1).run_one(tid)

    with session_scope(db_url) as s2:
        got = TaskService(s2).get(tid)
        assert got.status == TaskStatus.SUCCEEDED
        events = TaskService(s2).list_events(tid)
        assert len(events) >= 3
