"""Versioned Agent Execution Record contract (aer.v1).

Numbers are copied only from an explicit measured or estimate report.
Missing usage stays unknown. This module does not invent token or spend figures.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from max_agent.models.enums import CostSource, UsageMarker
from max_agent.models.schemas import UsageReport

AER_CONTRACT_VERSION = "aer.v1"

COST_SOURCES = (
    CostSource.GROK_BOT_QUOTA.value,
    CostSource.CLI_QUOTA.value,
    CostSource.API_SPEND.value,
)

_ACCEPTED_MARKERS = {UsageMarker.MEASURED.value, UsageMarker.ESTIMATE.value}
_KIND_BY_EVENT = {
    "created": "task_run",
    "inbound": "task_run",
    "attempt_started": "attempt",
    "attempt_finished": "attempt",
    "leased": "attempt",
    "verification": "verification",
}


class CostReading(BaseModel):
    source: str = CostSource.UNKNOWN.value
    marker: str = UsageMarker.UNKNOWN.value
    amount: float | None = None
    currency: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    provider: str | None = None


class WorkerStamp(BaseModel):
    worker_kind: str | None = None
    model: str | None = None
    provider: str | None = None
    runtime_ms: int | None = None
    runtime_marker: str = UsageMarker.UNKNOWN.value


class EvidenceLink(BaseModel):
    kind: str
    ref: str | None = None
    marker: str = UsageMarker.UNKNOWN.value
    attempt_no: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class AERRecord(BaseModel):
    """One durable export row. `kind` is task_run, attempt, event, or verification."""

    contract_version: str = AER_CONTRACT_VERSION
    kind: str
    task_id: str
    event_id: int
    event_type: str
    occurred_at: datetime
    attempt_id: str | None = None
    attempt_no: int | None = None
    issue_ref: str | None = None
    worker: WorkerStamp = Field(default_factory=WorkerStamp)
    cost: CostReading = Field(default_factory=CostReading)
    verification: dict[str, Any] | None = None
    evidence: list[EvidenceLink] = Field(default_factory=list)
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class MetricValue(BaseModel):
    marker: str = UsageMarker.UNKNOWN.value
    value: float | None = None
    reason: str = ""
    verified: int | None = None
    eligible: int | None = None


class EconomicsReport(BaseModel):
    contract_version: str = AER_CONTRACT_VERSION
    verified_task_rate: MetricValue
    cost_per_verified_task: MetricValue
    retry_overhead: MetricValue
    by_source: dict[str, MetricValue]


class BudgetView(BaseModel):
    status: str
    max_cost_units: float | None = None
    ceilings: dict[str, float | None]
    by_source: dict[str, str]
    measured_total: float | None = None
    reason: str = ""


class AttemptView(BaseModel):
    attempt_id: str
    attempt_no: int
    status: str
    worker_kind: str | None = None
    model: str | None = None
    provider: str | None = None
    runtime_ms: int | None = None
    runtime_marker: str = UsageMarker.UNKNOWN.value
    cost: CostReading
    failure_class: str | None = None


class ExportView(BaseModel):
    event_id: int
    kind: str
    contract_version: str
    status: str
    attempts: int
    last_error: str | None = None


class TaskTrace(BaseModel):
    contract_version: str = AER_CONTRACT_VERSION
    task_id: str
    status: str
    issue_ref: str | None = None
    chain: list[EvidenceLink]
    attempts: list[AttemptView]
    budget: BudgetView
    exports: list[ExportView]


class ExportRetryResult(BaseModel):
    delivered: int
    failed: int


def record_kind(event_type: str) -> str:
    return _KIND_BY_EVENT.get(event_type, "event")


def _text(value: Any, *, limit: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:limit]


def _nonneg_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number < 0 or number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _nonneg_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _source(value: Any) -> str:
    text = _text(value, limit=64)
    if text is None:
        return CostSource.UNKNOWN.value
    lowered = text.lower()
    if lowered in COST_SOURCES:
        return lowered
    return CostSource.UNKNOWN.value


def normalize_usage(raw: Any) -> CostReading:
    """Copy a worker usage report. Unmarked numbers are dropped, not guessed."""
    if isinstance(raw, UsageReport):
        data: dict[str, Any] = raw.model_dump()
    elif isinstance(raw, CostReading):
        data = raw.model_dump()
    elif isinstance(raw, dict):
        data = raw
    else:
        return CostReading()

    model = _text(data.get("model"))
    provider = _text(data.get("provider"))
    source = _source(data.get("source"))
    marker = (_text(data.get("marker"), limit=32) or "").lower()
    if marker not in _ACCEPTED_MARKERS:
        return CostReading(source=source, marker=UsageMarker.UNKNOWN.value, model=model, provider=provider)

    amount = _nonneg_float(data.get("amount"))
    if amount is None:
        return CostReading(source=source, marker=UsageMarker.UNKNOWN.value, model=model, provider=provider)

    currency = _text(data.get("currency"), limit=16)
    return CostReading(
        source=source,
        marker=marker,
        amount=amount,
        currency=currency,
        input_tokens=_nonneg_int(data.get("input_tokens")),
        output_tokens=_nonneg_int(data.get("output_tokens")),
        model=model,
        provider=provider,
    )


def usage_from_result(usage: Any, outputs: dict[str, Any] | None) -> CostReading:
    if usage is not None:
        return normalize_usage(usage)
    if isinstance(outputs, dict) and "usage" in outputs:
        return normalize_usage(outputs.get("usage"))
    return normalize_usage(None)


def explicit_issue_ref(issue_ref: str | None, tags: list[Any] | None) -> str | None:
    """Use only a caller-supplied ref or an `issue:` tag. Do not parse free text."""
    direct = _text(issue_ref, limit=256)
    if direct is not None:
        return direct
    for tag in tags or []:
        if not isinstance(tag, str):
            continue
        if tag.startswith("issue:"):
            found = _text(tag[6:], limit=256)
            if found is not None:
                return found
    return None


def ceilings_payload(ceilings: Any) -> dict[str, float]:
    raw = ceilings.model_dump() if hasattr(ceilings, "model_dump") else dict(ceilings or {})
    payload: dict[str, float] = {}
    for source in COST_SOURCES:
        value = _nonneg_float(raw.get(source))
        if value is not None and source in raw and raw.get(source) is not None:
            payload[source] = value
    return payload
