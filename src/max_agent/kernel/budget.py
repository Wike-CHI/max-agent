"""Transactional three-scope reservations; no unit conversion or guessed spend."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from max_agent.db.models import (
    BudgetHoldRow, BudgetPolicyRow, BudgetReservationRow,
    CostEntryRow, ProviderAccountRow, TaskAttemptRow, TaskRow, UsageRecordRow,
)
from max_agent.kernel.journal import Journal
from max_agent.models.enums import EventType

_UNIT = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_SCOPES = ("global", "provider", "task")


def _instant(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("budget time must include a timezone")
    return value.astimezone(timezone.utc)


def _stored(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _amount(value: Decimal | str | int | None) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("a finite nonnegative amount is required")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("a finite nonnegative amount is required") from None
    if not number.is_finite() or number < 0 or number.normalize().as_tuple().exponent < -18:
        raise ValueError("amount must be nonnegative with at most 18 decimal places")
    if number >= Decimal("1000000000000000000"):
        raise ValueError("amount exceeds storage precision")
    return number


class InsufficientBudgetError(ValueError):
    """No reservation or paid execution is permitted for this quote."""


class BudgetService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_policy(
        self, *, owner: str, scope: str, scope_id: str, unit: str,
        period_start: datetime, period_end: datetime,
        ceiling: Decimal | str | int, warning_at: Decimal | str | int | None = None,
    ) -> BudgetPolicyRow:
        if scope not in _SCOPES or not owner.strip() or not scope_id.strip():
            raise ValueError("invalid budget scope")
        if scope == "global" and scope_id != "all":
            raise ValueError("global scope_id must be all")
        if scope == "task":
            task = self.session.get(TaskRow, scope_id)
            if task is None or task.created_by != owner:
                raise ValueError("task budget owner mismatch")
        if scope == "provider" and not self.session.query(ProviderAccountRow).filter_by(
            owner=owner, provider_key=scope_id
        ).first():
            raise ValueError("provider has no account for owner")
        if not _UNIT.fullmatch(unit):
            raise ValueError("invalid budget unit")
        start, end = _instant(period_start), _instant(period_end)
        if start >= end:
            raise ValueError("budget period must have positive duration")
        cap = _amount(ceiling)
        warn = _amount(warning_at) if warning_at is not None else None
        if warn is not None and warn > cap:
            raise ValueError("warning threshold exceeds ceiling")
        candidates = self.session.query(BudgetPolicyRow).filter_by(
            owner=owner, scope=scope, scope_id=scope_id, unit=unit, enabled=True
        ).all()
        if any(start < _stored(item.period_end) and _stored(item.period_start) < end for item in candidates):
            raise ValueError("overlapping budget period")
        row = BudgetPolicyRow(
            id=str(uuid.uuid4()), owner=owner, scope=scope, scope_id=scope_id, unit=unit,
            period_start=start, period_end=end, ceiling=cap, warning_at=warn, enabled=True,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def _active_policy(self, owner: str, scope: str, scope_id: str, unit: str, now: datetime) -> BudgetPolicyRow:
        # PostgreSQL row locks serialize reservations for each scope, in a
        # fixed global → provider → task order. SQLite uses BEGIN IMMEDIATE.
        rows = self.session.query(BudgetPolicyRow).filter_by(
            owner=owner, scope=scope, scope_id=scope_id, unit=unit, enabled=True
        ).order_by(BudgetPolicyRow.id).with_for_update().all()
        active = [row for row in rows if _stored(row.period_start) <= now < _stored(row.period_end)]
        if len(active) != 1:
            raise InsufficientBudgetError(f"exactly one active {scope} budget in {unit} is required")
        return active[0]

    def reserve(
        self, *, owner: str, account_id: str, task_id: str, attempt_id: str,
        fencing_token: int, unit: str, upper_bound: Decimal | str | int,
        idempotency_key: str, now: datetime,
    ) -> BudgetReservationRow:
        amount = _amount(upper_bound)
        if not idempotency_key or len(idempotency_key) > 128 or not _UNIT.fullmatch(unit):
            raise ValueError("invalid reservation key or unit")
        prior = self.session.query(BudgetReservationRow).filter_by(idempotency_key=idempotency_key).one_or_none()
        if prior is not None:
            if (prior.owner, prior.attempt_id, prior.account_id, prior.task_id, prior.fencing_token, prior.unit, prior.reserved) != (
                owner, attempt_id, account_id, task_id, fencing_token, unit, amount
            ):
                raise ValueError("reservation key conflicts with a different request")
            return prior
        account = self.session.get(ProviderAccountRow, account_id)
        task = self.session.get(TaskRow, task_id)
        attempt = self.session.get(TaskAttemptRow, attempt_id)
        if account is None or task is None or attempt is None or not account.enabled:
            raise InsufficientBudgetError("account, task or attempt unavailable")
        if account.owner != owner or task.created_by != owner or attempt.task_id != task_id:
            raise InsufficientBudgetError("budget owner or attempt mismatch")
        if attempt.fencing_token != fencing_token or task.fencing_token != fencing_token:
            raise InsufficientBudgetError("stale attempt fence")
        instant = _instant(now)
        keys = (("global", "all"), ("provider", account.provider_key), ("task", task_id))
        policies = [self._active_policy(owner, scope, scope_id, unit, instant) for scope, scope_id in keys]
        for policy in policies:
            committed = sum(
                (hold.committed for hold in self.session.query(BudgetHoldRow).filter_by(policy_id=policy.id).all()),
                Decimal(0),
            )
            if committed + amount > policy.ceiling:
                raise InsufficientBudgetError(f"{policy.scope} budget exhausted for {unit}")
        row = BudgetReservationRow(
            id=str(uuid.uuid4()), owner=owner, account_id=account_id, task_id=task_id,
            attempt_id=attempt_id, fencing_token=fencing_token,
            idempotency_key=idempotency_key, unit=unit, reserved=amount, state="active",
        )
        self.session.add(row)
        self.session.flush()
        for policy in policies:
            self.session.add(BudgetHoldRow(
                id=str(uuid.uuid4()), reservation_id=row.id, policy_id=policy.id, committed=amount,
            ))
        self.session.flush()
        Journal(self.session).append(task_id, EventType.BUDGET_RESERVED, "budget reserved", {
            "reservation_id": row.id, "attempt_id": attempt_id, "unit": unit, "amount": str(amount),
            "policy_ids": [policy.id for policy in policies],
        })
        return row

    def _locked(self, reservation_id: str) -> BudgetReservationRow:
        row = self.session.query(BudgetReservationRow).filter_by(id=reservation_id).with_for_update().one_or_none()
        if row is None:
            raise KeyError("reservation not found")
        return row

    def _holds(self, reservation_id: str, amount: Decimal) -> None:
        for hold in self.session.query(BudgetHoldRow).filter_by(reservation_id=reservation_id).all():
            hold.committed = amount
        self.session.flush()

    def settle_from_entry(self, reservation_id: str, *, cost_entry_id: str) -> BudgetReservationRow:
        row = self._locked(reservation_id)
        entry = self.session.get(CostEntryRow, cost_entry_id)
        usage = self.session.get(UsageRecordRow, entry.usage_record_id) if entry is not None else None
        if entry is None or usage is None or usage.attempt_id != row.attempt_id:
            raise ValueError("cost entry does not belong to reserved attempt")
        if entry.confidence != "measured" or entry.unit != row.unit:
            return self.require_reconciliation(reservation_id, reason="cost is unmeasured or uses another unit")
        return self._settle_measured(row, amount=entry.amount, unit=entry.unit, evidence_ref=f"cost_entry:{entry.id}")

    def reconcile_measured(
        self, reservation_id: str, *, amount: Decimal | str | int,
        unit: str, evidence_ref: str,
    ) -> BudgetReservationRow:
        """Manual/provider-statement path for an interrupted attempt."""
        row = self._locked(reservation_id)
        if row.state == "settled" and row.actual == _amount(amount) and row.unit == unit:
            return row
        if row.state != "reconcile_required":
            raise ValueError("manual settlement requires a reconciliation state")
        return self._settle_measured(row, amount=amount, unit=unit, evidence_ref=evidence_ref)

    def _settle_measured(
        self, row: BudgetReservationRow, *, amount: Decimal | str | int,
        unit: str, evidence_ref: str,
    ) -> BudgetReservationRow:
        actual = _amount(amount)
        if unit != row.unit or not evidence_ref.strip():
            raise ValueError("measured cost must use the reserved unit and include evidence")
        if row.state == "settled":
            if row.actual != actual:
                raise ValueError("settlement conflicts with existing amount")
            return row
        if row.state == "released":
            raise ValueError("released reservation cannot be settled")
        row.actual = actual
        row.state = "settled"
        row.evidence_ref = evidence_ref[:256]
        self._holds(row.id, actual)
        event = EventType.BUDGET_OVERRUN if actual > row.reserved else EventType.BUDGET_SETTLED
        Journal(self.session).append(row.task_id, event, "budget settled", {
            "reservation_id": row.id, "attempt_id": row.attempt_id,
            "reserved": str(row.reserved), "actual": str(actual), "unit": unit,
        })
        return row

    def require_reconciliation(self, reservation_id: str, *, reason: str) -> BudgetReservationRow:
        row = self._locked(reservation_id)
        if row.state == "active":
            row.state = "reconcile_required"
            Journal(self.session).append(row.task_id, EventType.BUDGET_RECONCILIATION_REQUIRED, reason[:2000], {
                "reservation_id": row.id, "attempt_id": row.attempt_id, "unit": row.unit,
            })
        return row

    def release_uninvoked(self, reservation_id: str, *, evidence_ref: str) -> BudgetReservationRow:
        row = self._locked(reservation_id)
        if not evidence_ref.strip():
            raise ValueError("release requires evidence that execution did not occur")
        if row.state == "released":
            return row
        if row.state != "active":
            raise ValueError("unknown or settled spend requires reconciliation")
        row.state = "released"
        row.actual = Decimal(0)
        row.evidence_ref = evidence_ref[:256]
        self._holds(row.id, Decimal(0))
        Journal(self.session).append(row.task_id, EventType.BUDGET_RELEASED, "uninvoked reservation released", {
            "reservation_id": row.id, "attempt_id": row.attempt_id, "unit": row.unit,
        })
        return row
