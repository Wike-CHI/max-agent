"""AER contract, cost metrics, and budget ceilings. No paid API calls."""

from __future__ import annotations

from pathlib import Path

from max_agent.db.models import AERExportRow, TaskAttemptRow
from max_agent.kernel.aer import FileAERAdapter, set_aer_adapter
from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.models.aer import AER_CONTRACT_VERSION, normalize_usage
from max_agent.models.enums import TaskStatus, WorkerKind
from max_agent.models.schemas import BudgetCeilings, BudgetConstraints, TaskCreate


def _usage(source: str, marker: str, amount: float, **extra: object) -> dict:
    payload = {"source": source, "marker": marker, "amount": amount}
    payload.update(extra)
    return payload


def test_normalize_usage_does_not_invent_numbers():
    blank = normalize_usage(None)
    assert blank.marker == "unknown"
    assert blank.amount is None
    assert blank.input_tokens is None

    unmarked = normalize_usage({"source": "api_spend", "amount": 3, "input_tokens": 10})
    assert unmarked.marker == "unknown"
    assert unmarked.amount is None
    assert unmarked.input_tokens is None
    assert unmarked.source == "api_spend"

    measured = normalize_usage(
        {"source": "cli_quota", "marker": "measured", "amount": 1.25, "currency": "USD", "input_tokens": 4}
    )
    assert measured.marker == "measured"
    assert measured.amount == 1.25
    assert measured.input_tokens == 4
    assert measured.currency == "USD"


def test_trace_links_issue_worker_verification_and_pr(session):
    svc = TaskService(session)
    created = svc.create(
        TaskCreate(
            goal="ship the evidence chain",
            acceptance_criteria=["worker_ok"],
            worker_kind=WorkerKind.MOCK,
            issue_ref="Wike-CHI/max-agent#6",
            tags=["issue:ignored-because-explicit-ref-wins"],
            worker_payload={
                "mode": "success",
                "answer": "ok",
                "model": "fixture-model",
                "provider": "fixture-provider",
                "pr_url": "https://github.com/Wike-CHI/max-agent/pull/6",
                "usage": _usage(
                    "api_spend",
                    "measured",
                    0.4,
                    currency="USD",
                    input_tokens=11,
                    output_tokens=7,
                    model="fixture-model",
                    provider="fixture-provider",
                ),
            },
        )
    )
    finished = Scheduler(session).run_one(created.id)
    assert finished.status == TaskStatus.SUCCEEDED

    trace = svc.trace(created.id)
    assert trace.contract_version == AER_CONTRACT_VERSION
    assert trace.issue_ref == "Wike-CHI/max-agent#6"
    assert [link.kind for link in trace.chain] == ["issue", "worker", "verification", "pull_request"]
    assert trace.chain[0].ref == "Wike-CHI/max-agent#6"
    assert trace.chain[0].marker == "recorded"
    assert trace.chain[1].marker == "recorded"
    assert trace.chain[1].detail["worker_kind"] == "mock"
    assert trace.chain[1].detail["model"] == "fixture-model"
    assert trace.chain[1].detail["provider"] == "fixture-provider"
    assert trace.chain[1].detail["runtime_marker"] == "measured"
    assert trace.chain[1].detail["runtime_ms"] is not None
    assert trace.chain[1].detail["cost_marker"] == "measured"
    assert trace.chain[1].detail["cost_source"] == "api_spend"
    assert trace.chain[1].detail["cost_amount"] == 0.4
    assert trace.chain[2].detail["passed"] is True
    assert trace.chain[3].ref == "https://github.com/Wike-CHI/max-agent/pull/6"

    exports = session.query(AERExportRow).filter(AERExportRow.task_id == created.id).all()
    kinds = {row.kind for row in exports}
    assert {"task_run", "attempt", "event", "verification"} <= kinds
    assert all(row.contract_version == AER_CONTRACT_VERSION for row in exports)
    assert all(row.status == "exported" for row in exports)
    task_run = next(row for row in exports if row.kind == "task_run")
    assert task_run.body["issue_ref"] == "Wike-CHI/max-agent#6"
    assert task_run.body["contract_version"] == AER_CONTRACT_VERSION


def test_missing_usage_stays_unknown_and_does_not_block(session):
    svc = TaskService(session)
    created = svc.create(
        TaskCreate(
            goal="no meter",
            acceptance_criteria=["worker_ok"],
            worker_kind=WorkerKind.MOCK,
            worker_payload={"mode": "success", "answer": "ok"},
            budget=BudgetConstraints(ceilings=BudgetCeilings(api_spend=1)),
        )
    )
    finished = Scheduler(session).run_one(created.id)
    assert finished.status == TaskStatus.SUCCEEDED
    assert finished.attempts == 1

    trace = svc.trace(created.id)
    assert trace.chain[0].marker == "unknown"
    assert trace.chain[3].marker == "unknown"
    assert trace.attempts[0].cost.marker == "unknown"
    assert trace.attempts[0].cost.amount is None
    assert trace.budget.status == "unknown"

    report = svc.metrics()
    assert report.verified_task_rate.marker == "measured"
    assert report.verified_task_rate.value == 1
    assert report.cost_per_verified_task.marker == "unknown"
    assert report.cost_per_verified_task.value is None
    assert report.retry_overhead.marker == "measured"
    assert report.retry_overhead.value == 0
    assert report.by_source["api_spend"].value is None
    assert report.by_source["grok_bot_quota"].marker == "unknown"
    assert report.by_source["cli_quota"].marker == "unknown"


def test_measured_cost_metrics_and_source_split(session):
    svc = TaskService(session)
    created = svc.create(
        TaskCreate(
            goal="metered",
            acceptance_criteria=["worker_ok"],
            worker_kind=WorkerKind.MOCK,
            worker_payload={
                "mode": "fail_recoverable",
                "usage": _usage("grok_bot_quota", "measured", 2),
            },
            budget=BudgetConstraints(
                max_attempts=3,
                ceilings=BudgetCeilings(api_spend=0.01, grok_bot_quota=5, cli_quota=0.01),
            ),
        )
    )
    first = Scheduler(session).run_one(created.id)
    assert first.status == TaskStatus.RETRY_WAIT
    assert first.attempts == 1

    row = svc._get_row(created.id)
    row.worker_payload = {
        "mode": "success",
        "answer": "ok",
        "usage": _usage("grok_bot_quota", "measured", 1.5),
    }
    session.flush()
    second = Scheduler(session).run_one(created.id)
    assert second.status == TaskStatus.SUCCEEDED
    assert second.attempts == 2

    report = svc.metrics()
    assert report.cost_per_verified_task.marker == "measured"
    assert report.cost_per_verified_task.value == 3.5
    assert report.retry_overhead.value == 1
    assert report.by_source["grok_bot_quota"].marker == "measured"
    assert report.by_source["grok_bot_quota"].value == 3.5
    assert report.by_source["api_spend"].value == 0
    assert report.by_source["cli_quota"].value == 0
    assert svc.trace(created.id).budget.status == "ok"


def test_budget_ceiling_stops_next_claim_without_touching_fence(session):
    svc = TaskService(session)
    created = svc.create(
        TaskCreate(
            goal="over budget",
            acceptance_criteria=["worker_ok"],
            worker_kind=WorkerKind.MOCK,
            tags=["payment"],
            worker_payload={
                "mode": "fail_recoverable",
                "usage": _usage("api_spend", "measured", 1.5),
            },
            budget=BudgetConstraints(max_attempts=3, ceilings=BudgetCeilings(api_spend=1)),
        )
    )
    blocked = Scheduler(session).run_one(created.id)
    assert blocked.status == TaskStatus.BLOCKED
    assert blocked.attempts == 0
    assert svc._get_row(created.id).approved_at is None

    approved = svc.approve(created.id, actor="user", note="grant")
    assert approved.status == TaskStatus.READY
    ran = Scheduler(session).run_one(created.id)
    assert ran.status == TaskStatus.RETRY_WAIT
    assert ran.attempts == 1
    token = svc.get(created.id).fencing_token

    again = Scheduler(session).run_one(created.id)
    assert again.status == TaskStatus.RETRY_WAIT
    assert again.attempts == 1
    assert again.fencing_token == token
    assert "api_spend" in (again.last_error or "")
    assert "exceeds" in (again.last_error or "")
    events = svc.list_events(created.id)
    assert any(event.event_type.value == "budget_exceeded" for event in events)
    assert session.query(TaskAttemptRow).filter(TaskAttemptRow.task_id == created.id).count() == 1


def test_estimate_does_not_count_as_measured_spend(session):
    svc = TaskService(session)
    created = svc.create(
        TaskCreate(
            goal="estimate only",
            acceptance_criteria=["worker_ok"],
            worker_kind=WorkerKind.MOCK,
            worker_payload={
                "mode": "fail_recoverable",
                "usage": _usage("cli_quota", "estimate", 50),
            },
            budget=BudgetConstraints(max_attempts=3, ceilings=BudgetCeilings(cli_quota=1)),
        )
    )
    first = Scheduler(session).run_one(created.id)
    assert first.status == TaskStatus.RETRY_WAIT
    assert svc.trace(created.id).budget.status == "unknown"

    row = svc._get_row(created.id)
    row.worker_payload = {"mode": "success", "answer": "ok", "usage": _usage("cli_quota", "estimate", 4)}
    session.flush()
    second = Scheduler(session).run_one(created.id)
    assert second.status == TaskStatus.SUCCEEDED
    assert second.attempts == 2
    report = svc.metrics()
    assert report.cost_per_verified_task.marker == "estimate"
    assert report.cost_per_verified_task.value == 54
    assert report.by_source["cli_quota"].marker == "unknown"


def test_aggregate_ceiling_and_unattributed_spend(session):
    svc = TaskService(session)
    created = svc.create(
        TaskCreate(
            goal="aggregate cap",
            acceptance_criteria=["worker_ok"],
            worker_kind=WorkerKind.MOCK,
            worker_payload={"mode": "fail_recoverable", "usage": _usage("api_spend", "measured", 1.25)},
            budget=BudgetConstraints(max_attempts=3, max_cost_units=1),
        )
    )
    first = Scheduler(session).run_one(created.id)
    assert first.status == TaskStatus.RETRY_WAIT
    second = Scheduler(session).run_one(created.id)
    assert second.attempts == 1
    assert second.status == TaskStatus.RETRY_WAIT
    assert "aggregate" in (second.last_error or "")

    other = svc.create(
        TaskCreate(
            goal="unattributed",
            acceptance_criteria=["worker_ok"],
            worker_payload={"mode": "success", "usage": {"marker": "measured", "amount": 1.25}},
        )
    )
    assert Scheduler(session).run_one(other.id).status == TaskStatus.SUCCEEDED
    report = svc.metrics()
    assert report.cost_per_verified_task.marker == "measured"
    assert report.cost_per_verified_task.value == 1.25
    assert report.by_source["api_spend"].marker == "unknown"
    assert report.by_source["grok_bot_quota"].value is None
    assert report.by_source["cli_quota"].value is None


def test_export_failure_is_retryable_and_does_not_block_the_task(session, tmp_path: Path):
    class Boom:
        def export_record(self, record) -> None:
            raise RuntimeError("sink down")

    class Memory:
        def __init__(self) -> None:
            self.records = []

        def export_record(self, record) -> None:
            self.records.append(record)

    set_aer_adapter(Boom())
    svc = TaskService(session)
    created = svc.create(
        TaskCreate(
            goal="export outage",
            acceptance_criteria=["worker_ok"],
            worker_kind=WorkerKind.MOCK,
            worker_payload={"mode": "success", "answer": "ok"},
        )
    )
    assert created.status == TaskStatus.READY
    failed_rows = session.query(AERExportRow).all()
    assert failed_rows
    assert all(row.status == "failed" for row in failed_rows)
    assert all(row.attempts == 1 for row in failed_rows)

    finished = Scheduler(session).run_one(created.id)
    assert finished.status == TaskStatus.SUCCEEDED
    assert all(row.status == "failed" for row in session.query(AERExportRow).all())

    sink = Memory()
    set_aer_adapter(sink)
    result = svc.retry_aer_exports()
    assert result.failed == 0
    assert result.delivered == session.query(AERExportRow).count()
    assert all(row.status == "exported" for row in session.query(AERExportRow).all())
    assert svc.get(created.id).status == TaskStatus.SUCCEEDED
    assert sink.records
    assert {record.kind for record in sink.records} >= {"task_run", "attempt", "verification", "event"}

    directory = tmp_path / "aer"
    set_aer_adapter(FileAERAdapter(directory))
    for row in session.query(AERExportRow).all():
        row.status = "failed"
        row.last_error = "force"
    session.flush()
    again = svc.retry_aer_exports()
    files = sorted(path.name for path in directory.glob("*.json"))
    assert again.failed == 0
    assert len(files) == again.delivered
    for row in session.query(AERExportRow).all():
        row.status = "failed"
    session.flush()
    rewritten = svc.retry_aer_exports()
    assert rewritten.delivered == len(files)
    assert sorted(path.name for path in directory.glob("*.json")) == files


def test_issue_tag_and_http_trace(db_url: str):
    from max_agent.db.session import session_scope

    with session_scope(db_url) as session:
        svc = TaskService(session)
        created = svc.create(
            TaskCreate(
                goal="from a tag",
                acceptance_criteria=["worker_ok"],
                tags=["issue:Wike-CHI/max-agent#6"],
                worker_payload={"mode": "success"},
            )
        )
        assert svc.get(created.id).issue_ref == "Wike-CHI/max-agent#6"
        Scheduler(session).run_one(created.id)
        task_id = created.id

    from fastapi.testclient import TestClient

    from max_agent.api.app import create_app

    client = TestClient(create_app())
    traced = client.get(f"/v1/tasks/{task_id}/trace")
    assert traced.status_code == 200
    body = traced.json()
    assert body["issue_ref"] == "Wike-CHI/max-agent#6"
    assert body["chain"][0]["kind"] == "issue"
    assert body["chain"][2]["kind"] == "verification"
    metrics = client.get("/v1/metrics")
    assert metrics.status_code == 200
    assert metrics.json()["verified_task_rate"]["marker"] == "measured"
    missing = client.get("/v1/tasks/missing/trace")
    assert missing.status_code == 404
