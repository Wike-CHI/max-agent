"""Retry wait and side-effect replay policy."""

from __future__ import annotations


def backoff_delay_seconds(attempt_no: int, *, base: float, cap: float) -> float:
    """Exponential delay after `attempt_no` completed attempts. base<=0 means immediate."""
    if base <= 0 or attempt_no <= 0:
        return 0.0
    delay = base * (2 ** (attempt_no - 1))
    if cap > 0:
        delay = min(delay, cap)
    return float(delay)
