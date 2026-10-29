"""Issue #7: task DAG, fair queue, and an approval gate distinct from cancel and timeout."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from max_agent.db.models import TaskAttemptRow, TaskRow, utcnow
from max_agent.db.session import session_scope
from max_agent.kernel.errors import ConcurrencyLimitError, CyclicDependencyError
from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.models.enums import FailureClass, TaskStatus
from max_agent.models.schemas import GraphNode, ScheduleRequest, TaskCreate
from max_agent.policy.boundary import irreversible_refusal, tool_is_irreversible
from max_agent.workers.base import Worker, WorkerContext
from max_agent.workers.mock import MockWorker
from max_agent.models.schemas import WorkerResult


def _node(key: str, depends_on: list[str], *, priority: int = 100) -> GraphNode:
    return GraphNode(
        key=key,
        goal=f"step {key}",
        acceptance_criteria=["worker_ok"],
        priority=priority,
        depends_on=depends_on,
        worker_payload={"mode": "success", "answer": key},
    )


def _chain(count: int) -> list[GraphNode]:
    nodes: list[GraphNode] = []
    for index in range(count):
        deps = [] if index == 0 else [f"n{index - 1:02d}"]
        nodes.append(_node(f"n{index:02d}", deps))
    return nodes


class _CountingWorker(Worker):
    kind = MockWorker.kind
    calls = 0

    def capabilities(self) -> set[str]:
        return set()

    def run(self, ctx: WorkerContext) -> WorkerResult:
        type(self).calls += 1
        raise AssertionError("worker must not run")


class _ExternalAfterReject(Worker):
    kind = MockWorker.kind

    def __init__(self, marker: Path, db_url: str) -> None:
        self.marker = marker
        self.db_url = db_url

    def capabilities(self) -> set[str]:
        return set()

    def run(self, ctx: WorkerContext) -> WorkerResult:
        with session_scope(self.db_url) as session:
            row = session.get(TaskRow, ctx.task_id)
            assert row is not None
            row.approval_decision = "rejected"
        if ctx.on_tool_call:
            ctx.on_tool_call("external_write", {"target": "prod"})
        self.marker.write_text("sent", encoding="utf-8")
        return WorkerResult(ok=True, summary="sent", outputs={"answer": "sent"})


def test_cycle_is_rejected_and_nothing_is_stored(session):
    svc = TaskService(session)
    with pytest.raises(CyclicDependencyError):
        svc.create_graph(
            [
                _node("a", ["b"]),
                _node("b", ["c"]),
                _node("c", ["a"]),
            ]
        )
    assert svc.list_tasks() == []

    with pytest.raises(CyclicDependencyError):
        svc.create_graph([_node("loop", ["loop"])])
    assert svc.list_tasks() == []


def test_join_wakes_only_after_every_dependency_succeeds(session):
    svc = TaskService(session)
    created = svc.create_graph(
        [
            _node("left", []),
            _node("right", []),
            _node("join", ["left", "right"]),
        ]
    )
    by_key = {key: task for key, task in zip(["left", "right", "join"], created, strict=True)}
    assert by_key["join"].status == TaskStatus.BLOCKED

    Scheduler(session).run_one(by_key["left"].id)
    assert svc.get(by_key["join"].id).status == TaskStatus.BLOCKED

    Scheduler(session).run_one(by_key["right"].id)
    assert svc.get(by_key["join"].id).status == TaskStatus.READY

    finished = Scheduler(session).run_one(by_key["join"].id)
    assert finished.status == TaskStatus.SUCCEEDED


def test_graph_of_22_restarts_and_resumes(session):
    svc = TaskService(session)
    created = svc.create_graph(_chain(22))
    assert len(created) == 22
    assert created[0].status == TaskStatus.READY
    assert created[1].status == TaskStatus.BLOCKED

    first = Scheduler(session, worker_id="boot-1").run_queue(max_tasks=6)
    assert len(first) == 6
    assert [task.status for task in first] == [TaskStatus.SUCCEEDED] * 6

    resumed = Scheduler(session, worker_id="boot-2").run_queue()
    assert len(resumed) == 16
    stored = {task.id: task for task in svc.list_tasks(limit=50)}
    assert len(stored) == 22
    for task in stored.values():
        assert task.status == TaskStatus.SUCCEEDED
        assert task.attempts == 1
        assert task.failure_class is None


def test_expired_lease_in_graph_resumes_without_a_second_success(session):
    svc = TaskService(session)
    created = svc.create_graph(_chain(22))
    root_id = created[0].id
    Scheduler(session, worker_id="lost").acquire_lease(root_id)
    row = svc._get_row(root_id)
    assert row.status == TaskStatus.RUNNING.value
    row.lease_expires_at = utcnow() - timedelta(seconds=5)
    session.flush()

    done = Scheduler(session, worker_id="restart", backoff_base_seconds=0, backoff_cap_seconds=0).run_queue()
    assert len(done) == 22
    root = svc.get(root_id)
    assert root.status == TaskStatus.SUCCEEDED
    assert root.attempts == 2
    attempts = (
        session.query(TaskAttemptRow)
        .filter(TaskAttemptRow.task_id == root_id)
        .order_by(TaskAttemptRow.attempt_no.asc())
        .all()
    )
    assert [item.status for item in attempts] == ["interrupted", "succeeded"]
    assert int(attempts[0].fencing_token) != int(attempts[1].fencing_token)
    for task in created[1:]:
        restored = svc.get(task.id)
        assert restored.status == TaskStatus.SUCCEEDED
        assert restored.attempts == 1


def test_fairness_runs_a_starved_lower_priority_task_first(session):
    svc = TaskService(session)
    urgent = svc.create(
        TaskCreate(goal="urgent", acceptance_criteria=["worker_ok"], priority=1, worker_payload={"mode": "success"})
    )
    starved = svc.create(
        TaskCreate(goal="starved", acceptance_criteria=["worker_ok"], priority=80, worker_payload={"mode": "success"})
    )
    row = svc._get_row(starved.id)
    row.created_at = utcnow() - timedelta(hours=2)
    session.flush()

    picked = Scheduler(session, starve_after_seconds=60, fairness_step=50).run_one(None)
    assert picked.id == starved.id
    assert svc.get(urgent.id).status == TaskStatus.READY


def test_priority_still_wins_before_the_fairness_window(session):
    svc = TaskService(session)
    urgent = svc.create(
        TaskCreate(goal="urgent", acceptance_criteria=["worker_ok"], priority=1, worker_payload={"mode": "success"})
    )
    svc.create(
        TaskCreate(goal="later", acceptance_criteria=["worker_ok"], priority=80, worker_payload={"mode": "success"})
    )
    picked = Scheduler(session, starve_after_seconds=86400, fairness_step=50).run_one(None)
    assert picked.id == urgent.id


def test_concurrency_limit_does_not_start_a_second_lease(session):
    svc = TaskService(session)
    first = svc.create(TaskCreate(goal="holds the slot", acceptance_criteria=["worker_ok"]))
    second = svc.create(TaskCreate(goal="must wait", acceptance_criteria=["worker_ok"]))
    held = Scheduler(session, worker_id="holder", max_concurrency=1).acquire_lease(first.id)
    assert held.fencing_token >= 1

    _CountingWorker.calls = 0
    follower = _CountingWorker()
    with pytest.raises(ConcurrencyLimitError):
        Scheduler(
            session,
            worker_id="follower",
            max_concurrency=1,
            worker_resolver=lambda _kind: follower,
        ).run_one(second.id)
    assert follower.calls == 0
    assert svc.get(second.id).status == TaskStatus.READY
    assert svc.get(first.id).fencing_token == held.fencing_token


def test_approval_cancel_and_timeout_are_not_the_same_outcome(session):
    svc = TaskService(session)
    waiting = svc.create(
        TaskCreate(
            goal="publish later",
            acceptance_criteria=["worker_ok"],
            tags=["publish"],
            has_side_effects=True,
            worker_payload={"mode": "success"},
        )
    )
    assert waiting.status == TaskStatus.WAITING_APPROVAL
    granted = svc.approve(waiting.id, actor="sam", note="limited", scope=["publish"], ttl_seconds=60)
    assert granted.status == TaskStatus.READY
    assert granted.approval_decision == "granted"
    assert granted.approved_by == "sam"
    assert granted.approval_scope == ["publish"]
    assert granted.approval_expires_at is not None

    row = svc._get_row(waiting.id)
    row.approval_expires_at = utcnow() - timedelta(seconds=5)
    session.flush()
    _CountingWorker.calls = 0
    expired = Scheduler(session, worker_resolver=lambda _kind: _CountingWorker()).run_one(waiting.id)
    assert expired.status == TaskStatus.WAITING_APPROVAL
    assert expired.approval_decision == "expired"
    assert expired.failure_class is None
    assert expired.cancel_requested_at is None
    expired_types = [event.event_type.value for event in svc.list_events(waiting.id)]
    assert "approval_expired" in expired_types
    assert "cancelled" not in expired_types

    cancelled = svc.cancel(
        svc.create(TaskCreate(goal="stop", acceptance_criteria=["worker_ok"], worker_payload={"mode": "success"})).id
    )
    assert cancelled.status == TaskStatus.CANCELLED
    assert cancelled.failure_class is None
    assert cancelled.approval_decision is None
    assert cancelled.cancel_requested_at is not None
    assert cancelled.status != expired.status

    timed = Scheduler(session).run_one(
        svc.create(
            TaskCreate(
                goal="slow",
                acceptance_criteria=["worker_ok"],
                timeout_seconds=1,
                worker_payload={"mode": "sleep", "delay_seconds": 30},
                budget={"max_attempts": 3, "timeout_seconds": 1},
            )
        ).id
    )
    assert timed.failure_class == FailureClass.TIMEOUT.value
    assert timed.status == TaskStatus.RETRY_WAIT
    assert timed.status not in {TaskStatus.CANCELLED, TaskStatus.WAITING_APPROVAL}
    assert timed.cancel_requested_at is None
    assert timed.approval_decision is None


def test_rejected_approval_does_not_execute_external_work(session, tmp_path: Path):
    marker = tmp_path / "charged.txt"
    svc = TaskService(session)
    task = svc.create(
        TaskCreate(
            goal="charge card",
            acceptance_criteria=["worker_ok"],
            tags=["payment"],
            has_side_effects=True,
            worker_payload={"mode": "success"},
        )
    )
    rejected = svc.reject(task.id, actor="lead", note="no")
    assert rejected.status == TaskStatus.WAITING_APPROVAL
    assert rejected.approval_decision == "rejected"
    assert rejected.approved_at is None
    assert rejected.failure_class is None

    _CountingWorker.calls = 0
    worker = _CountingWorker()
    held = Scheduler(session, worker_resolver=lambda _kind: worker).run_one(
        task.id,
        ScheduleRequest(confirm_side_effects=True),
    )
    assert worker.calls == 0
    assert held.status == TaskStatus.WAITING_APPROVAL
    assert not marker.exists()
    types = [event.event_type.value for event in svc.list_events(task.id)]
    assert "approval_rejected" in types
    assert "attempt_started" not in types
    assert "tool_call" not in types


def test_irreversible_tool_is_refused_after_rejection(session, db_url: str, tmp_path: Path):
    marker = tmp_path / "external.txt"
    svc = TaskService(session)
    task = svc.create(
        TaskCreate(goal="maybe write", acceptance_criteria=["worker_ok"], worker_payload={"mode": "success"})
    )
    worker = _ExternalAfterReject(marker, db_url)
    out = Scheduler(session, worker_resolver=lambda _kind: worker).run_one(task.id)
    assert not marker.exists()
    assert out.status != TaskStatus.SUCCEEDED
    events = svc.list_events(task.id)
    assert all(event.payload.get("args", {}).get("target") != "prod" for event in events if event.event_type.value == "tool_call")
    assert any("approval rejected" in (event.message or "") for event in events)


def test_scope_must_cover_sensitive_tags_before_a_run(session):
    svc = TaskService(session)
    task = svc.create(
        TaskCreate(
            goal="ship",
            acceptance_criteria=["worker_ok"],
            tags=["publish", "external_write"],
            worker_payload={"mode": "success"},
        )
    )
    with pytest.raises(ValueError, match="scope"):
        svc.approve(task.id, actor="sam", scope=["publish"])
    assert svc.get(task.id).status == TaskStatus.WAITING_APPROVAL

    _CountingWorker.calls = 0
    worker = _CountingWorker()
    blocked = Scheduler(session, worker_resolver=lambda _kind: worker).run_one(
        task.id,
        ScheduleRequest(confirm_side_effects=True),
    )
    assert worker.calls == 0
    assert blocked.status == TaskStatus.WAITING_APPROVAL


def test_refusal_reasons_do_not_collapse_into_timeout():
    now = utcnow()
    assert tool_is_irreversible("external_write", {}) is True
    assert tool_is_irreversible("mock.echo", {}) is False
    assert irreversible_refusal(status="cancelled", approval_decision=None, cancel_requested_at=now) == "cancelled"
    assert irreversible_refusal(status="running", approval_decision="rejected", cancel_requested_at=None) == "approval rejected"
    assert irreversible_refusal(status="running", approval_decision="expired", cancel_requested_at=None) == "approval expired"
    assert irreversible_refusal(status="running", approval_decision="granted", cancel_requested_at=None) is None
    assert irreversible_refusal(status="retry_wait", approval_decision=None, cancel_requested_at=None) is None
