"""Versioned observed usage, independent of legacy Float cost projections."""

from __future__ import annotations

import math
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from max_agent.db.models import CostEntryRow, TaskAttemptRow, UsageRecordRow
from max_agent.models.aer import CostReading
from max_agent.models.schemas import UsageReport

USAGE_CONTRACT_VERSION = "usage.v1"
_BILLING_MODES = {"api_metered", "subscription", "prepaid", "local_compute", "unknown"}


def _label(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _decimal_amount(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    # Matches Numeric(36, 18); never silently round a source value.
    if not amount.is_finite() or amount < 0 or amount.normalize().as_tuple().exponent < -18:
        return None
    if amount >= Decimal("1000000000000000000"):
        return None
    return amount


def record_attempt_usage(
    session: Session,
    attempt: TaskAttemptRow,
    *,
    raw: UsageReport | dict[str, Any] | None,
    reading: CostReading,
) -> UsageRecordRow:
    """Store one final report per attempt, even when cost is unknown.

    The deterministic key prevents a replay from billing twice. The caller
    checks the task fence before this function and commits with finalization.
    """
    event_key = f"attempt:{attempt.id}:final"
    existing = session.query(UsageRecordRow).filter_by(source_event_key=event_key).one_or_none()
    if existing is not None:
        return existing
    data = raw.model_dump() if isinstance(raw, UsageReport) else raw if isinstance(raw, dict) else {}
    mode = _label(data.get("billing_mode"), 32)
    if mode not in _BILLING_MODES:
        mode = "unknown"
    record = UsageRecordRow(
        id=str(uuid.uuid4()),
        attempt_id=attempt.id,
        source_event_key=event_key,
        contract_version=USAGE_CONTRACT_VERSION,
        provider=reading.provider,
        model=reading.model,
        billing_mode=mode,
        source=reading.source,
        input_tokens=reading.input_tokens,
        output_tokens=reading.output_tokens,
        cached_input_tokens=reading.cached_input_tokens,
        reasoning_tokens=reading.reasoning_tokens,
        compute_seconds=_seconds(data.get("compute_seconds")),
    )
    session.add(record)
    session.flush()

    # The legacy CostReading validates the marker and amount. Use the raw
    # value for exact decimal storage, never the rounded Float projection.
    amount = _decimal_amount(data.get("amount")) if reading.amount is not None else None
    if amount is not None:
        session.add(
            CostEntryRow(
                id=str(uuid.uuid4()),
                usage_record_id=record.id,
                amount=amount,
                unit=_label(data.get("unit"), 32) or _label(data.get("currency"), 32),
                confidence=reading.marker,
                source=reading.source,
                billing_mode=mode,
            )
        )
        session.flush()
    return record
