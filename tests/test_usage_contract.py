"""v0.6 usage contract: explicit observations, no invented bill or currency."""

from __future__ import annotations

from decimal import Decimal

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select

from max_agent.db.models import CostEntryRow, TaskAttemptRow, TaskRow, UsageRecordRow
from max_agent.db.session import reset_engine, session_scope
from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.kernel.usage import record_attempt_usage
from max_agent.models.aer import normalize_usage
from max_agent.models.enums import TaskStatus
from max_agent.models.schemas import TaskCreate


def _run(session, usage: dict) -> tuple[TaskAttemptRow, UsageRecordRow]:
    task = TaskService(session).create(
        TaskCreate(goal="record observed usage", acceptance_criteria=["worker_ok"], worker_payload={"usage": usage})
    )
    assert Scheduler(session).run_one(task.id).status == TaskStatus.SUCCEEDED
    attempt = session.query(TaskAttemptRow).filter_by(task_id=task.id).one()
    record = session.query(UsageRecordRow).filter_by(attempt_id=attempt.id).one()
    return attempt, record


def test_token_only_report_is_durable_and_replay_does_not_create_a_bill(session) -> None:
    raw = {
        "source": "cli_quota",
        "input_tokens": 120,
        "output_tokens": 20,
        "cached_input_tokens": 80,
        "reasoning_tokens": 5,
        "amount": 999,  # no marker: this is not a cost reading
        "billing_mode": "subscription",
    }
    attempt, record = _run(session, raw)
    assert record.contract_version == "usage.v1"
    assert record.billing_mode == "subscription"
    assert (record.input_tokens, record.output_tokens, record.cached_input_tokens, record.reasoning_tokens) == (
        120, 20, 80, 5
    )
    assert attempt.cost_marker == "unknown"
    assert attempt.cost_amount is None
    assert session.query(CostEntryRow).count() == 0

    again = record_attempt_usage(session, attempt, raw=raw, reading=normalize_usage(raw))
    assert again.id == record.id
    assert session.query(UsageRecordRow).count() == 1
    assert session.query(CostEntryRow).count() == 0


def test_original_amount_and_unit_are_kept_separate_from_legacy_float(session) -> None:
    _, api = _run(
        session,
        {"source": "api_spend", "marker": "measured", "amount": 0.12345678, "currency": "USD",
         "billing_mode": "api_metered", "input_tokens": 10},
    )
    _, subscription = _run(
        session,
        {"source": "cli_quota", "marker": "estimate", "amount": 4, "unit": "requests",
         "billing_mode": "subscription"},
    )
    _, prepaid = _run(
        session,
        {"source": "api_spend", "marker": "measured", "amount": 1.25, "unit": "credits",
         "billing_mode": "prepaid"},
    )
    _, local = _run(session, {"billing_mode": "local_compute", "compute_seconds": 12.5})
    entries = {entry.usage_record_id: entry for entry in session.query(CostEntryRow).all()}
    assert entries[api.id].amount == Decimal("0.12345678")
    assert entries[api.id].unit == "USD"
    assert entries[api.id].billing_mode == "api_metered"
    assert entries[subscription.id].amount == Decimal("4")
    assert entries[subscription.id].unit == "requests"
    assert entries[subscription.id].confidence == "estimate"
    assert entries[prepaid.id].unit == "credits"
    assert entries[prepaid.id].billing_mode == "prepaid"
    assert local.compute_seconds == 12.5
    assert local.id not in entries

    _, tiny = _run(session, {"source": "api_spend", "marker": "measured", "amount": 1e-12,
                             "currency": "USD", "billing_mode": "api_metered"})
    session.expire_all()
    assert session.query(CostEntryRow).filter_by(usage_record_id=tiny.id).one().amount == Decimal("0.000000000001")


def test_additive_migration_preserves_existing_task_and_downgrades(tmp_path, monkeypatch) -> None:
    url = f"sqlite:///{tmp_path / 'v06-migrated.db'}"
    monkeypatch.setenv("MAX_AGENT_DATABASE_URL", url)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "f19a0c3d8e21")
    reset_engine()
    with session_scope(url) as session:
        task_id = TaskService(session).create(TaskCreate(goal="legacy task")).id
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    assert {"usage_records", "cost_entries"} <= set(inspect(engine).get_table_names())
    amount_column = next(col for col in inspect(engine).get_columns("cost_entries") if col["name"] == "amount")
    assert "VARCHAR" in str(amount_column["type"]).upper()
    with engine.connect() as conn:
        assert conn.execute(select(TaskRow.goal).where(TaskRow.id == task_id)).scalar_one() == "legacy task"
    command.downgrade(cfg, "f19a0c3d8e21")
    assert "usage_records" not in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.execute(select(TaskRow.goal).where(TaskRow.id == task_id)).scalar_one() == "legacy task"
    engine.dispose()
    reset_engine()
