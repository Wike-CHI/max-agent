"""Task dependency graph: cycle rejection and wake-up when predecessors succeed."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from max_agent.db.models import TaskRow
from max_agent.kernel.errors import CyclicDependencyError
from max_agent.kernel.journal import Journal
from max_agent.kernel.mutate import transition_task
from max_agent.models.enums import TaskStatus
from max_agent.policy.approval import HumanHold, approval_hold


def dependency_edges(session: Session) -> dict[str, list[str]]:
    rows = session.query(TaskRow).all()
    return {row.id: [str(item) for item in (row.dependencies or [])] for row in rows}


def find_cycle(edges: dict[str, list[str]]) -> list[str] | None:
    """Return one cycle as node ids (start repeated at the end), or None."""
    color: dict[str, int] = {}
    for node, deps in edges.items():
        color.setdefault(node, 0)
        for dep in deps:
            color.setdefault(dep, 0)
    parent: dict[str, str] = {}

    def dfs(node: str) -> list[str] | None:
        color[node] = 1
        for dep in edges.get(node, []):
            if color.get(dep, 0) == 1:
                cycle = [dep, node]
                cursor = node
                while cursor != dep:
                    cursor = parent.get(cursor, dep)
                    cycle.append(cursor)
                    if len(cycle) > len(color) + 1:
                        break
                cycle.reverse()
                if cycle[0] != cycle[-1]:
                    cycle.append(cycle[0])
                return cycle
            if color.get(dep, 0) == 0:
                parent[dep] = node
                found = dfs(dep)
                if found is not None:
                    return found
        color[node] = 2
        return None

    for node in list(color):
        if color[node] == 0:
            found = dfs(node)
            if found is not None:
                return found
    return None


def assert_acyclic(edges: dict[str, list[str]]) -> None:
    cycle = find_cycle(edges)
    if cycle is not None:
        raise CyclicDependencyError(cycle)


def deps_satisfied(session: Session, row: TaskRow) -> bool:
    for dep_id in list(row.dependencies or []):
        dep = session.get(TaskRow, str(dep_id))
        if dep is None or TaskStatus(dep.status) != TaskStatus.SUCCEEDED:
            return False
    return True


def _hold_for(row: TaskRow, now: datetime) -> HumanHold:
    return approval_hold(
        tags=list(row.tags or []),
        has_side_effects=bool(row.has_side_effects),
        is_retry=int(row.attempts) > 0 or TaskStatus(row.status) == TaskStatus.RETRY_WAIT,
        confirm_side_effects=False,
        allow_auto_retry_side_effects=bool(row.allow_auto_retry_side_effects),
        idempotency_key=row.idempotency_key,
        approved_at=row.approved_at,
        approval_decision=row.approval_decision,
        approval_expires_at=row.approval_expires_at,
        approval_scope=list(row.approval_scope or []),
        now=now,
    )


def wake_satisfied(session: Session, *, now: datetime) -> list[str]:
    """Move blocked tasks whose dependencies all succeeded.

    Side-effect confirmation stays blocked. An approval gate becomes
    `waiting_approval` instead of being mixed into `blocked`.
    """
    journal = Journal(session)
    rows = (
        session.query(TaskRow)
        .filter(TaskRow.status.in_([TaskStatus.BLOCKED.value, TaskStatus.WAITING_APPROVAL.value]))
        .all()
    )
    woken: list[str] = []
    for row in rows:
        if not deps_satisfied(session, row):
            continue
        hold = _hold_for(row, now)
        target = hold.status or TaskStatus.READY
        current = TaskStatus(row.status)
        if current == target:
            continue
        extra: dict[str, object] = {}
        if target == TaskStatus.READY:
            extra["last_error"] = None
            message = "dependencies satisfied"
        else:
            extra["last_error"] = hold.reason
            message = hold.reason
            if hold.decision:
                extra["approval_decision"] = hold.decision
        row = transition_task(session, row, target, message, extra=extra, journal=journal)
        if hold.event is not None and target != TaskStatus.READY:
            journal.append(row.id, hold.event, hold.reason, {"woken": True})
        woken.append(row.id)
    return woken
