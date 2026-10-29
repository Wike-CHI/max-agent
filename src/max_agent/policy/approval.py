"""Human approval hooks (policy model — no real payments)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from max_agent.models.enums import EventType, TaskStatus


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


SENSITIVE_TAGS = frozenset(
    {
        "delete",
        "deploy",
        "payment",
        "publish",
        "permission",
        "production",
        "external_write",
    }
)


@dataclass
class ApprovalPolicy:
    """MVP policy: classify whether an action needs human confirmation."""

    require_for_side_effects_retry: bool = True


def sensitive_tags(tags: list[str]) -> list[str]:
    """Remote write, publish, and paid actions. Sorted for stable messages."""
    found = SENSITIVE_TAGS.intersection({t.lower() for t in tags})
    return sorted(found)


def requires_human_approval(
    *,
    tags: list[str],
    has_side_effects: bool,
    is_retry: bool,
    confirm_side_effects: bool,
    allow_auto_retry_side_effects: bool,
    idempotency_key: str | None = None,
    approval_granted: bool = False,
) -> tuple[bool, str]:
    """Sensitive tags need a persisted approve() grant.

    Passing confirm_side_effects on run is not that grant. It still confirms
    a side-effect retry for tasks that are not in the sensitive set.
    """
    sensitive = sensitive_tags(tags)
    if sensitive and not approval_granted:
        return True, f"sensitive tags require approval: {sensitive}"

    if has_side_effects and is_retry:
        if (
            confirm_side_effects
            or allow_auto_retry_side_effects
            or (idempotency_key or "").strip()
            or approval_granted
        ):
            return False, (
                "side-effect retry allowed by confirmation, budget, idempotency key, or prior approval"
            )
        return True, "side-effect task retry requires confirm_side_effects=true"

    if sensitive:
        return False, f"sensitive tags approved: {sensitive}"
    return False, "ok"


def grant_is_active(
    *,
    approved_at: datetime | None,
    approval_decision: str | None,
    approval_expires_at: datetime | None,
    approval_scope: list[str] | None,
    tags: list[str],
    now: datetime,
) -> bool:
    """A persisted approve() grant that still covers this task."""
    if approval_decision in {"rejected", "expired"}:
        return False
    if approved_at is None:
        return False
    expires = _as_utc(approval_expires_at)
    current = _as_utc(now)
    if expires is not None and current is not None and expires <= current:
        return False
    required = set(sensitive_tags(tags))
    scope = {item.lower() for item in (approval_scope or [])}
    if required and scope and not required.issubset(scope):
        return False
    return True


@dataclass(frozen=True)
class HumanHold:
    """Why a task must not start. `status` None means the approval gate is clear."""

    status: TaskStatus | None
    reason: str
    event: EventType | None
    decision: str | None


def approval_hold(
    *,
    tags: list[str],
    has_side_effects: bool,
    is_retry: bool,
    confirm_side_effects: bool,
    allow_auto_retry_side_effects: bool,
    idempotency_key: str | None,
    approved_at: datetime | None,
    approval_decision: str | None,
    approval_expires_at: datetime | None,
    approval_scope: list[str] | None,
    now: datetime,
) -> HumanHold:
    """Separate approval rejection, expiry, and the older side-effect confirmation block.

    `confirm_side_effects` still cannot grant a sensitive tag. A rejected or expired
    grant is not a timeout and not a cancel.
    """
    if approval_decision == "rejected":
        return HumanHold(TaskStatus.WAITING_APPROVAL, "approval rejected", EventType.APPROVAL_REJECTED, "rejected")

    expires = _as_utc(approval_expires_at)
    current = _as_utc(now)
    grant_lapsed = (
        expires is not None
        and current is not None
        and expires <= current
        and (approved_at is not None or approval_decision == "granted")
    )
    if grant_lapsed or approval_decision == "expired":
        return HumanHold(TaskStatus.WAITING_APPROVAL, "approval expired", EventType.APPROVAL_EXPIRED, "expired")

    granted = grant_is_active(
        approved_at=approved_at,
        approval_decision=approval_decision,
        approval_expires_at=approval_expires_at,
        approval_scope=approval_scope,
        tags=tags,
        now=now,
    )
    need, reason = requires_human_approval(
        tags=tags,
        has_side_effects=has_side_effects,
        is_retry=is_retry,
        confirm_side_effects=confirm_side_effects,
        allow_auto_retry_side_effects=allow_auto_retry_side_effects,
        idempotency_key=idempotency_key,
        approval_granted=granted,
    )
    if not need:
        return HumanHold(None, "ok", None, None)
    if sensitive_tags(tags) and not granted:
        return HumanHold(TaskStatus.WAITING_APPROVAL, reason, EventType.APPROVAL_REQUIRED, None)
    return HumanHold(TaskStatus.BLOCKED, reason, EventType.APPROVAL_REQUIRED, None)
