"""Map a task row to the API read model."""

from __future__ import annotations

from max_agent.db.models import TaskRow
from max_agent.models.enums import TaskStatus, WorkerKind
from max_agent.models.aer import COST_SOURCES
from max_agent.models.schemas import BudgetCeilings, BudgetConstraints, TaskRead


def _ceiling(raw: dict, source: str) -> float | None:
    if source not in raw or raw.get(source) is None:
        return None
    value = raw.get(source)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def task_to_read(row: TaskRow) -> TaskRead:
    raw_ceilings = dict(row.budget_ceilings or {})
    budget = BudgetConstraints(
        max_attempts=row.max_attempts,
        timeout_seconds=row.timeout_seconds,
        max_cost_units=row.max_cost_units,
        allow_auto_retry_side_effects=row.allow_auto_retry_side_effects,
        ceilings=BudgetCeilings(**{source: _ceiling(raw_ceilings, source) for source in COST_SOURCES}),
    )
    return TaskRead(
        id=row.id,
        goal=row.goal,
        acceptance_criteria=list(row.acceptance_criteria or []),
        priority=row.priority,
        dependencies=list(row.dependencies or []),
        status=TaskStatus(row.status),
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        timeout_seconds=row.timeout_seconds,
        budget=budget,
        idempotency_key=row.idempotency_key,
        worker_kind=WorkerKind(row.worker_kind),
        worker_payload=dict(row.worker_payload or {}),
        has_side_effects=bool(row.has_side_effects),
        tags=list(row.tags or []),
        result_summary=row.result_summary,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        leased_by=row.leased_by,
        lease_expires_at=row.lease_expires_at,
        fencing_token=int(row.fencing_token or 0),
        next_retry_at=row.next_retry_at,
        failure_class=row.failure_class,
        approved_at=row.approved_at,
        approved_by=row.approved_by,
        approval_note=row.approval_note,
        issue_ref=row.issue_ref,
    )
