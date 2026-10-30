"""Ready-queue selection: priority, aging fairness, and a concurrency cap.

Lower `priority` still runs first. A task that has waited through
`starve_after_seconds` sheds `fairness_step` priority points per window so a
slow high-priority stream cannot starve it. The cap counts unexpired
in-flight leases, the same rows fencing tokens already protect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from max_agent.db.models import TaskRow
from max_agent.kernel.lease import active_inflight_count
from max_agent.kernel.timeutil import as_utc
from max_agent.models.enums import TaskStatus


@dataclass(frozen=True)
class QueuePolicy:
    max_concurrency: int = 1
    starve_after_seconds: float = 3600.0
    fairness_step: int = 10


@dataclass(frozen=True)
class QueuePick:
    task_id: str | None
    reason: str  # selected, empty, concurrency


def effective_priority(priority: int, created_at: datetime | None, now: datetime, policy: QueuePolicy) -> int:
    created = as_utc(created_at)
    current = as_utc(now)
    waited = 0.0
    if created is not None and current is not None:
        waited = max(0.0, (current - created).total_seconds())
    window = policy.starve_after_seconds
    if window <= 0 or policy.fairness_step <= 0:
        boost = 0
    else:
        boost = int(waited // window) * policy.fairness_step
    return int(priority) - boost


def pick_next(
    session: Session,
    now: datetime,
    exclude: set[str],
    policy: QueuePolicy | None = None,
    *,
    created_by: str | None = None,
) -> QueuePick:
    policy = policy or QueuePolicy()
    if active_inflight_count(session, now) >= policy.max_concurrency:
        return QueuePick(None, "concurrency")
    query = session.query(TaskRow).filter(
        TaskRow.status.in_([TaskStatus.READY.value, TaskStatus.RETRY_WAIT.value])
    )
    if created_by is not None:
        query = query.filter(TaskRow.created_by == created_by)
    rows = query.all()
    current = as_utc(now)
    eligible: list[TaskRow] = []
    for row in rows:
        if row.id in exclude:
            continue
        nxt = as_utc(row.next_retry_at)
        if nxt is not None and current is not None and nxt > current:
            continue
        eligible.append(row)
    if not eligible:
        return QueuePick(None, "empty")
    eligible.sort(
        key=lambda row: (
            effective_priority(int(row.priority), row.created_at, now, policy),
            as_utc(row.created_at) or current,
            row.id,
        )
    )
    return QueuePick(eligible[0].id, "selected")
