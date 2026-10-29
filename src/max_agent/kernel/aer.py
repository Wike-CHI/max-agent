"""AER adapters. Export I/O must not decide task success."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.orm import Session

from max_agent.db.models import AERExportRow, TaskAttemptRow, TaskEventRow, TaskRow
from max_agent.models.aer import (
    AER_CONTRACT_VERSION,
    AERRecord,
    CostReading,
    EvidenceLink,
    WorkerStamp,
    normalize_usage,
    record_kind,
)
from max_agent.models.enums import UsageMarker

_override: AERAdapter | None = None


class AERAdapter(Protocol):
    def export_record(self, record: AERRecord) -> None: ...


class NullAERAdapter:
    """Accept the record and keep it only in the outbox."""

    def export_record(self, record: AERRecord) -> None:
        return None

    def export_event(self, event: TaskEventRow) -> None:
        return None


class FileAERAdapter:
    """Idempotent directory sink: one JSON file per event id, overwritten on retry."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def export_record(self, record: AERRecord) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{record.event_id}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(record.model_dump_json(), encoding="utf-8")
        temporary.replace(target)

    def export_event(self, event: TaskEventRow) -> None:
        return None


def set_aer_adapter(adapter: AERAdapter | None) -> None:
    global _override
    _override = adapter


def get_aer_adapter() -> AERAdapter:
    if _override is not None:
        return _override
    directory = os.environ.get("MAX_AGENT_AER_DIR", "").strip()
    if directory:
        return FileAERAdapter(directory)
    return NullAERAdapter()


def _cost_from_attempt(attempt: TaskAttemptRow | None) -> CostReading:
    if attempt is None:
        return CostReading()
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


def _load_attempt(session: Session, task_id: str, payload: dict[str, Any]) -> TaskAttemptRow | None:
    attempt_id = payload.get("attempt_id")
    if isinstance(attempt_id, str) and attempt_id:
        found = session.get(TaskAttemptRow, attempt_id)
        if found is not None:
            return found
    number = payload.get("attempt")
    if isinstance(number, int):
        return (
            session.query(TaskAttemptRow)
            .filter(TaskAttemptRow.task_id == task_id, TaskAttemptRow.attempt_no == number)
            .one_or_none()
        )
    return None


def _worker_stamp(payload: dict[str, Any], attempt: TaskAttemptRow | None, task: TaskRow | None) -> WorkerStamp:
    raw = payload.get("worker") if isinstance(payload.get("worker"), dict) else {}
    runtime = raw.get("runtime_ms")
    if not isinstance(runtime, int) or runtime < 0:
        runtime = attempt.runtime_ms if attempt is not None else None
    runtime_ms = runtime if isinstance(runtime, int) and runtime >= 0 else None
    worker_kind = _text(raw.get("worker_kind"))
    if worker_kind is None and attempt is not None and attempt.worker_kind:
        worker_kind = attempt.worker_kind
    if worker_kind is None and task is not None:
        worker_kind = task.worker_kind
    model = _text(raw.get("model")) or (attempt.model if attempt else None)
    provider = _text(raw.get("provider")) or (attempt.provider if attempt else None)
    return WorkerStamp(
        worker_kind=worker_kind,
        model=model,
        provider=provider,
        runtime_ms=runtime_ms,
        runtime_marker=UsageMarker.MEASURED.value if runtime_ms is not None else UsageMarker.UNKNOWN.value,
    )


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _evidence(task: TaskRow | None, event: TaskEventRow, payload: dict[str, Any]) -> list[EvidenceLink]:
    links: list[EvidenceLink] = []
    issue_ref = task.issue_ref if task is not None else None
    if issue_ref:
        links.append(EvidenceLink(kind="issue", ref=issue_ref, marker="recorded"))
    if event.event_type == "verification":
        links.append(
            EvidenceLink(
                kind="verification",
                ref=str(event.id),
                marker="recorded",
                detail={"passed": payload.get("passed")},
            )
        )
    return links


def build_aer_record(session: Session, event: TaskEventRow) -> AERRecord:
    payload = dict(event.payload or {})
    task = session.get(TaskRow, event.task_id)
    attempt = _load_attempt(session, event.task_id, payload)
    if "cost" in payload:
        cost = normalize_usage(payload.get("cost"))
    else:
        cost = _cost_from_attempt(attempt)
    attempt_no = payload.get("attempt")
    if not isinstance(attempt_no, int) and attempt is not None:
        attempt_no = attempt.attempt_no
    attempt_id = payload.get("attempt_id")
    if not isinstance(attempt_id, str):
        attempt_id = attempt.id if attempt is not None else None
    verification = payload if event.event_type == "verification" else None
    return AERRecord(
        contract_version=AER_CONTRACT_VERSION,
        kind=record_kind(event.event_type),
        task_id=event.task_id,
        event_id=int(event.id),
        event_type=event.event_type,
        occurred_at=event.created_at,
        attempt_id=attempt_id,
        attempt_no=attempt_no if isinstance(attempt_no, int) else None,
        issue_ref=task.issue_ref if task is not None else None,
        worker=_worker_stamp(payload, attempt, task),
        cost=cost,
        verification=verification,
        evidence=_evidence(task, event, payload),
        message=event.message or "",
        payload=payload,
    )


def fallback_record(event: TaskEventRow, error: str) -> AERRecord:
    return AERRecord(
        contract_version=AER_CONTRACT_VERSION,
        kind="event",
        task_id=event.task_id,
        event_id=int(event.id),
        event_type=event.event_type,
        occurred_at=event.created_at,
        message=event.message or "",
        payload={"contract_error": error[:500]},
    )


def call_adapter(adapter: AERAdapter, record: AERRecord) -> None:
    export_record = getattr(adapter, "export_record", None)
    if callable(export_record):
        export_record(record)
        return
    legacy = getattr(adapter, "export_event", None)
    if callable(legacy):
        legacy(record)
        return
    raise TypeError("AER adapter has no export_record")


def deliver_export(adapter: AERAdapter, row: AERExportRow, record: AERRecord) -> None:
    """Mark the outbox row. Adapter errors stay on the row and are not raised."""
    try:
        call_adapter(adapter, record)
    except Exception as exc:  # noqa: BLE001 — export must not abort the task transaction
        row.status = "failed"
        row.attempts = int(row.attempts or 0) + 1
        row.last_error = str(exc)[:1000]
        row.exported_at = None
        return
    row.status = "exported"
    row.attempts = int(row.attempts or 0) + 1
    row.last_error = None
    row.exported_at = record.occurred_at
