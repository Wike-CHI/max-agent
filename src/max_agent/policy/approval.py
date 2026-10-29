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


def requires_human_approval(
    *,
    tags: list[str],
    has_side_effects: bool,
    is_retry: bool,
    confirm_side_effects: bool,
    allow_auto_retry_side_effects: bool,
    idempotency_key: str | None = None,
) -> tuple[bool, str]:
    sensitive = SENSITIVE_TAGS.intersection({t.lower() for t in tags})
    if sensitive:
        if confirm_side_effects:
            return False, f"sensitive tags approved: {sorted(sensitive)}"
        return True, f"sensitive tags require approval: {sorted(sensitive)}"

    if has_side_effects and is_retry:
        if confirm_side_effects or allow_auto_retry_side_effects or (idempotency_key or "").strip():
            return False, "side-effect retry allowed by confirmation, budget, or idempotency key"
        return True, "side-effect task retry requires confirm_side_effects=true"

    return False, "ok"
