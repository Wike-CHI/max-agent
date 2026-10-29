"""Human approval hooks (policy model — no real payments)."""

from __future__ import annotations

from dataclasses import dataclass


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
