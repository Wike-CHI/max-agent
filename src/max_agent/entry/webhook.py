"""Signed inbound events from a webhook or Grok Routine.

The caller gets a task_id back. This module does not run a worker and does not poll.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from sqlalchemy.orm import Session

from max_agent.db.models import InboundDeliveryRow, utcnow
from max_agent.entry.contract import envelope
from max_agent.kernel.journal import Journal
from max_agent.kernel.service import TaskService
from max_agent.models.enums import EventType
from max_agent.models.schemas import InboundEvent, TaskCreate

MAX_SKEW_SECONDS = 300
MAX_BODY_BYTES = 1_000_000


class WebhookError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def sign_body(secret: str, timestamp: str, body: bytes) -> str:
    message = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_signature(
    *,
    secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
    now: float | None = None,
) -> None:
    """Fail closed. Missing secret, bad signature, and stale timestamps share 401 except the disabled case."""
    if not secret:
        raise WebhookError(503, "webhook ingress disabled")
    if len(body) > MAX_BODY_BYTES:
        raise WebhookError(400, "payload too large")
    try:
        seen = int(timestamp)
    except (TypeError, ValueError):
        raise WebhookError(401, "invalid signature") from None
    current = time.time() if now is None else now
    if abs(current - seen) > MAX_SKEW_SECONDS:
        raise WebhookError(401, "invalid signature")
    presented = signature.strip()
    if presented.lower().startswith("sha256="):
        presented = presented.split("=", 1)[1]
    presented = presented.lower()
    expected = sign_body(secret, str(timestamp).strip(), body)
    if len(presented) != len(expected) or not hmac.compare_digest(expected, presented):
        raise WebhookError(401, "invalid signature")


def accept_inbound(session: Session, *, delivery_id: str, body: bytes, event: InboundEvent) -> dict:
    """Create or reuse a task and remember the delivery id."""
    digest = hashlib.sha256(body).hexdigest()
    existing = session.get(InboundDeliveryRow, delivery_id)
    service = TaskService(session)
    if existing is not None:
        if existing.body_sha256 != digest:
            raise WebhookError(409, "delivery id already used with a different body")
        task = service.get(existing.task_id)
        return envelope(command="webhook", task=task, deduplicated=True)

    task = service.create(
        TaskCreate(
            goal=event.goal,
            acceptance_criteria=list(event.acceptance_criteria),
            priority=event.priority,
            worker_kind=event.worker_kind,
            worker_payload=dict(event.worker_payload),
            idempotency_key=event.idempotency_key,
            has_side_effects=event.has_side_effects,
            tags=list(event.tags),
        )
    )
    session.add(
        InboundDeliveryRow(
            delivery_id=delivery_id,
            idempotency_key=event.idempotency_key,
            task_id=task.id,
            body_sha256=digest,
            received_at=utcnow(),
        )
    )
    Journal(session).append(
        task.id,
        EventType.INBOUND,
        "inbound event associated",
        {
            "delivery_id": delivery_id,
            "idempotency_key": event.idempotency_key,
            "idempotent_hit": bool(task.idempotent_replay),
        },
    )
    session.flush()
    return envelope(command="webhook", task=task, deduplicated=False)
