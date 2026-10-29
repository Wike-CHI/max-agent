"""Boundary in front of irreversible tool calls.

Workers must call this before the external effect. Cancel, approval rejection,
and approval expiry each refuse the call. A worker timeout is not one of those
signals and is not decided here.
"""

from __future__ import annotations

from datetime import datetime

_IRREVERSIBLE_TOKENS = (
    "delete",
    "deploy",
    "payment",
    "publish",
    "permission",
    "production",
    "external_write",
    "external.",
)


def tool_is_irreversible(name: str, args: dict | None = None) -> bool:
    payload = args or {}
    if payload.get("irreversible") is True:
        return True
    lowered = (name or "").lower()
    return any(token in lowered for token in _IRREVERSIBLE_TOKENS)


def irreversible_refusal(
    *,
    status: str,
    approval_decision: str | None,
    cancel_requested_at: datetime | None,
) -> str | None:
    """Return the refusal reason, or None when the tool may proceed."""
    if status == "cancelled" or cancel_requested_at is not None:
        return "cancelled"
    if approval_decision == "rejected":
        return "approval rejected"
    if approval_decision == "expired":
        return "approval expired"
    return None
