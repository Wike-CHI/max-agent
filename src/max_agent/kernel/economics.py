"""Budget checks, per-task evidence traces, and cost metrics.

A missing reading is `unknown`. This module never substitutes zero for a
reading the worker did not report.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from max_agent.db.models import AERExportRow, TaskAttemptRow, TaskEventRow, TaskRow
from max_agent.models.aer import (
    AER_CONTRACT_VERSION,
    COST_SOURCES,
    AttemptView,
    BudgetView,
    CostReading,
    EconomicsReport,
    EvidenceLink,
    ExportView,
    MetricValue,
    TaskTrace,
)
from max_agent.models.enums import TaskStatus, UsageMarker

_PR_KEYS = ("pr_url", "pull_request")
_ARTIFACT_KEYS = ("evidence_url", "artifact")
_ISSUE_OUTPUT_KEYS = ("issue_ref", "issue_url")


def _nonneg(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number < 0 or number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _ceilings(row: TaskRow) -> dict[str, float | None]:
    raw = dict(row.budget_ceilings or {})
    parsed: dict[str, float | None] = {}
    for source in COST_SOURCES:
        if source not in raw or raw.get(source) is None:
            parsed[source] = None
            continue
        parsed[source] = _nonneg(raw.get(source))
    return parsed


def _aggregate_ceiling(row: TaskRow) -> float | None:
    value = _nonneg(row.max_cost_units)
    if value is None or value == 0:
        return None
    return value


def _attempts(session: Session, task_id: str) -> list[TaskAttemptRow]:
    return (
        session.query(TaskAttemptRow)
        .filter(TaskAttemptRow.task_id == task_id)
        .order_by(TaskAttemptRow.attempt_no.asc())
        .all()
    )


def _reading(attempt: TaskAttemptRow) -> CostReading:
    return CostReading(
        source=attempt.cost_source or "unknown",
        marker=attempt.cost_marker or UsageMarker.UNKNOWN.value,
        amount=attempt.cost_amount,
        currency=attempt.cost_currency,
        input_tokens=attempt.input_tokens,
        output_tokens=attempt.output_tokens,
        model=attempt.model,
        provider=attempt.provider,
    )


def _is_measured(attempt: TaskAttemptRow) -> bool:
    return attempt.cost_marker == UsageMarker.MEASURED.value and attempt.cost_amount is not None


def _is_estimate(attempt: TaskAttemptRow) -> bool:
    return attempt.cost_marker == UsageMarker.ESTIMATE.value and attempt.cost_amount is not None


class BudgetAssessment:
    def __init__(
        self,
        *,
        status: str,
        max_cost_units: float | None,
        ceilings: dict[str, float | None],
        by_source: dict[str, str],
        measured_total: float | None,
        reason: str,
    ) -> None:
        self.status = status
        self.max_cost_units = max_cost_units
        self.ceilings = ceilings
        self.by_source = by_source
        self.measured_total = measured_total
        self.reason = reason

    def view(self) -> BudgetView:
        return BudgetView(
            status=self.status,
            max_cost_units=self.max_cost_units,
            ceilings=self.ceilings,
            by_source=self.by_source,
            measured_total=self.measured_total,
            reason=self.reason,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "max_cost_units": self.max_cost_units,
            "ceilings": self.ceilings,
            "by_source": self.by_source,
            "measured_total": self.measured_total,
        }


def _ledger_status(measured_sum: float, unknown: bool, ceiling: float | None) -> str:
    if ceiling is None:
        return "unset"
    if measured_sum > ceiling:
        return "exceeded"
    if unknown:
        return "unknown"
    return "ok"


def assess_budget(session: Session, row: TaskRow) -> BudgetAssessment:
    attempts = _attempts(session, row.id)
    ceilings = _ceilings(row)
    aggregate_cap = _aggregate_ceiling(row)
    by_source: dict[str, str] = {}
    reasons: list[str] = []

    fully_measured = bool(attempts) and all(_is_measured(attempt) for attempt in attempts)
    measured_values = [float(attempt.cost_amount) for attempt in attempts if _is_measured(attempt)]
    measured_total = sum(measured_values) if fully_measured else None
    aggregate_unknown = any(not _is_measured(attempt) for attempt in attempts)
    aggregate_status = _ledger_status(sum(measured_values), aggregate_unknown, aggregate_cap)
    if aggregate_status == "exceeded":
        reasons.append(f"aggregate measured {sum(measured_values):g} exceeds ceiling {aggregate_cap:g}")

    for source in COST_SOURCES:
        ceiling = ceilings[source]
        measured_sum = 0.0
        unknown = False
        for attempt in attempts:
            if _is_measured(attempt) and attempt.cost_source == source:
                measured_sum += float(attempt.cost_amount)
                continue
            if _is_measured(attempt) and attempt.cost_source in COST_SOURCES:
                continue
            # Unmarked, estimated, or unattributed spend might still belong here.
            if ceiling is not None:
                unknown = True
        status = _ledger_status(measured_sum, unknown, ceiling)
        by_source[source] = status
        if status == "exceeded":
            reasons.append(f"{source} measured {measured_sum:g} exceeds ceiling {ceiling:g}")

    statuses = [aggregate_status, *by_source.values()]
    if any(item == "exceeded" for item in statuses):
        overall = "exceeded"
    elif any(item == "unknown" for item in statuses):
        overall = "unknown"
    elif any(item == "ok" for item in statuses):
        overall = "ok"
    else:
        overall = "unset"
    if overall == "unknown" and not reasons:
        reasons.append("budget data is unknown")
    return BudgetAssessment(
        status=overall,
        max_cost_units=aggregate_cap,
        ceilings=ceilings,
        by_source=by_source,
        measured_total=measured_total,
        reason="; ".join(reasons),
    )


def _events(session: Session, task_id: str) -> list[TaskEventRow]:
    return (
        session.query(TaskEventRow)
        .filter(TaskEventRow.task_id == task_id)
        .order_by(TaskEventRow.id.asc())
        .all()
    )


def _string_values(payload: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    outputs = payload.get("outputs")
    sources: list[Any] = [payload]
    if isinstance(outputs, dict):
        sources.append(outputs)
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                found.append(value.strip())
    return found


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def build_trace(session: Session, row: TaskRow) -> TaskTrace:
    attempts = _attempts(session, row.id)
    events = _events(session, row.id)
    issue_ref = row.issue_ref
    if not issue_ref:
        for event in events:
            refs = _string_values(dict(event.payload or {}), _ISSUE_OUTPUT_KEYS)
            if refs:
                issue_ref = refs[0]
                break
    chain: list[EvidenceLink] = [
        EvidenceLink(
            kind="issue",
            ref=issue_ref,
            marker="recorded" if issue_ref else UsageMarker.UNKNOWN.value,
        )
    ]
    if attempts:
        for attempt in attempts:
            reading = _reading(attempt)
            chain.append(
                EvidenceLink(
                    kind="worker",
                    ref=attempt.id,
                    marker="recorded",
                    attempt_no=attempt.attempt_no,
                    detail={
                        "worker_kind": attempt.worker_kind or row.worker_kind,
                        "model": attempt.model,
                        "provider": attempt.provider,
                        "runtime_ms": attempt.runtime_ms,
                        "runtime_marker": (
                            UsageMarker.MEASURED.value if attempt.runtime_ms is not None else UsageMarker.UNKNOWN.value
                        ),
                        "cost_source": reading.source,
                        "cost_marker": reading.marker,
                        "cost_amount": reading.amount,
                    },
                )
            )
    else:
        chain.append(EvidenceLink(kind="worker", ref=None, marker=UsageMarker.UNKNOWN.value))

    verifications = [event for event in events if event.event_type == "verification"]
    if verifications:
        for event in verifications:
            payload = dict(event.payload or {})
            chain.append(
                EvidenceLink(
                    kind="verification",
                    ref=str(event.id),
                    marker="recorded",
                    detail={"passed": payload.get("passed"), "message": event.message},
                )
            )
    else:
        chain.append(EvidenceLink(kind="verification", ref=None, marker=UsageMarker.UNKNOWN.value))

    pr_refs: list[str] = []
    artifact_refs: list[str] = []
    for event in events:
        payload = dict(event.payload or {})
        pr_refs.extend(_string_values(payload, _PR_KEYS))
        artifact_refs.extend(_string_values(payload, _ARTIFACT_KEYS))
    pr_refs = _unique(pr_refs)
    artifact_refs = _unique(artifact_refs)
    if pr_refs:
        for ref in pr_refs:
            chain.append(EvidenceLink(kind="pull_request", ref=ref, marker="recorded"))
    else:
        chain.append(EvidenceLink(kind="pull_request", ref=None, marker=UsageMarker.UNKNOWN.value))
    for ref in artifact_refs:
        chain.append(EvidenceLink(kind="artifact", ref=ref, marker="recorded"))

    exports = (
        session.query(AERExportRow)
        .filter(AERExportRow.task_id == row.id)
        .order_by(AERExportRow.id.asc())
        .all()
    )
    return TaskTrace(
        contract_version=AER_CONTRACT_VERSION,
        task_id=row.id,
        status=row.status,
        issue_ref=issue_ref,
        chain=chain,
        attempts=[
            AttemptView(
                attempt_id=attempt.id,
                attempt_no=attempt.attempt_no,
                status=attempt.status,
                worker_kind=attempt.worker_kind or row.worker_kind,
                model=attempt.model,
                provider=attempt.provider,
                runtime_ms=attempt.runtime_ms,
                runtime_marker=(
                    UsageMarker.MEASURED.value if attempt.runtime_ms is not None else UsageMarker.UNKNOWN.value
                ),
                cost=_reading(attempt),
                failure_class=attempt.failure_class,
            )
            for attempt in attempts
        ],
        budget=assess_budget(session, row).view(),
        exports=[
            ExportView(
                event_id=item.event_id,
                kind=item.kind,
                contract_version=item.contract_version,
                status=item.status,
                attempts=item.attempts,
                last_error=item.last_error,
            )
            for item in exports
        ],
    )


def _unknown(reason: str) -> MetricValue:
    return MetricValue(marker=UsageMarker.UNKNOWN.value, value=None, reason=reason)


def _task_total(attempts: list[TaskAttemptRow]) -> tuple[str, float | None]:
    if not attempts:
        return UsageMarker.UNKNOWN.value, None
    if all(_is_measured(attempt) for attempt in attempts):
        return UsageMarker.MEASURED.value, sum(float(attempt.cost_amount) for attempt in attempts)
    if all(_is_estimate(attempt) for attempt in attempts):
        return UsageMarker.ESTIMATE.value, sum(float(attempt.cost_amount) for attempt in attempts)
    return UsageMarker.UNKNOWN.value, None


def _average(parts: list[tuple[str, float | None]]) -> MetricValue:
    if not parts:
        return _unknown("no verified tasks")
    markers = {marker for marker, _ in parts}
    if markers == {UsageMarker.MEASURED.value}:
        values = [value for _, value in parts if value is not None]
        return MetricValue(marker=UsageMarker.MEASURED.value, value=sum(values) / len(values))
    if markers == {UsageMarker.ESTIMATE.value}:
        values = [value for _, value in parts if value is not None]
        return MetricValue(
            marker=UsageMarker.ESTIMATE.value,
            value=sum(values) / len(values),
            reason="every verified task reported an estimate",
        )
    return _unknown("verified task is missing a complete usage reading")


def _source_average(groups: list[list[TaskAttemptRow]], source: str) -> MetricValue:
    parts: list[tuple[str, float | None]] = []
    for attempts in groups:
        if not attempts or not all(_is_measured(attempt) for attempt in attempts):
            return _unknown("verified task is missing a measured reading")
        if any(attempt.cost_source not in COST_SOURCES for attempt in attempts):
            return _unknown("measured usage is not attributed to a ledger")
        total = sum(float(attempt.cost_amount) for attempt in attempts if attempt.cost_source == source)
        parts.append((UsageMarker.MEASURED.value, total))
    return _average(parts)


def compute_metrics(session: Session) -> EconomicsReport:
    tasks = session.query(TaskRow).all()
    eligible = [task for task in tasks if task.status != TaskStatus.CANCELLED.value]
    verified = [task for task in eligible if task.status == TaskStatus.SUCCEEDED.value]
    if not eligible:
        rate = _unknown("no eligible tasks")
    else:
        rate = MetricValue(
            marker=UsageMarker.MEASURED.value,
            value=len(verified) / len(eligible),
            verified=len(verified),
            eligible=len(eligible),
        )
    if not verified:
        empty = _unknown("no verified tasks")
        return EconomicsReport(
            verified_task_rate=rate,
            cost_per_verified_task=empty,
            retry_overhead=empty,
            by_source={source: empty for source in COST_SOURCES},
        )

    grouped = [_attempts(session, task.id) for task in verified]
    if any(len(attempts) == 0 for attempts in grouped):
        retry = _unknown("verified task has no attempt row")
    else:
        extra = sum(len(attempts) for attempts in grouped) - len(verified)
        retry = MetricValue(marker=UsageMarker.MEASURED.value, value=extra / len(verified))
    return EconomicsReport(
        verified_task_rate=rate,
        cost_per_verified_task=_average([_task_total(attempts) for attempts in grouped]),
        retry_overhead=retry,
        by_source={source: _source_average(grouped, source) for source in COST_SOURCES},
    )
