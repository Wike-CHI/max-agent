"""Execution journal. Each event is also an AER outbox row.

Adapter failures are stored on the outbox row and retried later. They do not
propagate to TaskService, the lease, or verification.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from max_agent.db.models import AERExportRow, TaskEventRow, utcnow
from max_agent.kernel.aer import (
    AERAdapter,
    build_aer_record,
    deliver_export,
    fallback_record,
    get_aer_adapter,
)
from max_agent.models.aer import AERRecord, ExportRetryResult
from max_agent.models.enums import EventType


class Journal:
    def __init__(self, session: Session, aer: AERAdapter | None = None) -> None:
        self.session = session
        self._aer = aer

    @property
    def aer(self) -> AERAdapter:
        """Look up the process adapter on each delivery so a retry can swap sinks."""
        if self._aer is not None:
            return self._aer
        return get_aer_adapter()

    def append(
        self,
        task_id: str,
        event_type: EventType | str,
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> TaskEventRow:
        row = TaskEventRow(
            task_id=task_id,
            event_type=str(event_type),
            message=message,
            payload=payload or {},
            created_at=utcnow(),
        )
        self.session.add(row)
        self.session.flush()
        self._enqueue(row)
        return row

    def list_for_task(self, task_id: str, *, limit: int = 200) -> list[TaskEventRow]:
        return (
            self.session.query(TaskEventRow)
            .filter(TaskEventRow.task_id == task_id)
            .order_by(TaskEventRow.id.asc())
            .limit(limit)
            .all()
        )

    def retry_exports(self, *, limit: int = 500) -> ExportRetryResult:
        rows = (
            self.session.query(AERExportRow)
            .filter(AERExportRow.status.in_(("pending", "failed")))
            .order_by(AERExportRow.id.asc())
            .limit(limit)
            .all()
        )
        delivered = 0
        failed = 0
        for row in rows:
            record = AERRecord.model_validate(row.body)
            deliver_export(self.aer, row, record)
            if row.status == "exported":
                delivered += 1
            else:
                failed += 1
        self.session.flush()
        return ExportRetryResult(delivered=delivered, failed=failed)

    def _enqueue(self, event: TaskEventRow) -> None:
        try:
            record = build_aer_record(self.session, event)
            failed = False
            error = None
        except Exception as exc:  # noqa: BLE001 — a bad contract build must not drop the task write
            record = fallback_record(event, str(exc))
            failed = True
            error = f"contract build failed: {exc}"[:1000]
        export = AERExportRow(
            task_id=event.task_id,
            event_id=event.id,
            contract_version=record.contract_version,
            kind=record.kind,
            body=record.model_dump(mode="json"),
            status="failed" if failed else "pending",
            attempts=1 if failed else 0,
            last_error=error,
            created_at=utcnow(),
        )
        self.session.add(export)
        self.session.flush()
        if failed:
            return
        deliver_export(self.aer, export, record)
        self.session.flush()
