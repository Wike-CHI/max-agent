"""Stable JSON envelope for CLI, HTTP status, and the MCP facade."""

from __future__ import annotations

from typing import Any

from max_agent.db.models import utcnow
from max_agent.models.enums import EventType
from max_agent.models.schemas import TaskEventRead, TaskRead
from max_agent.policy.approval import grant_is_active, sensitive_tags


def latest_verification(events: list[TaskEventRead] | None) -> dict[str, Any] | None:
    if not events:
        return None
    for event in reversed(events):
        if event.event_type == EventType.VERIFICATION:
            return dict(event.payload or {})
    return None


def envelope(
    *,
    command: str,
    task: TaskRead | None = None,
    events: list[TaskEventRead] | None = None,
    ok: bool = True,
    error: str | None = None,
    deduplicated: bool = False,
) -> dict[str, Any]:
    """Fixed key set so a machine caller can parse every command the same way."""
    approved = False
    if task is not None:
        approved = grant_is_active(
            approved_at=task.approved_at,
            approval_decision=task.approval_decision,
            approval_expires_at=task.approval_expires_at,
            approval_scope=list(task.approval_scope or []),
            tags=list(task.tags),
            now=utcnow(),
        )
    approval_required = bool(task is not None and sensitive_tags(list(task.tags)) and not approved)
    return {
        "ok": ok,
        "command": command,
        "task_id": None if task is None else task.id,
        "status": None if task is None else task.status.value,
        "approval_required": approval_required,
        "approved": approved,
        "idempotent_hit": False if task is None else bool(task.idempotent_replay),
        "deduplicated": deduplicated,
        "idempotency_key": None if task is None else task.idempotency_key,
        "verification": latest_verification(events),
        "error": error,
        "task": None if task is None else task.model_dump(mode="json"),
        "events": None if events is None else [item.model_dump(mode="json") for item in events],
    }
