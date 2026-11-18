"""Real SQLite transactions for owner-scoped, three-level budget commitments."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from max_agent.db.models import BudgetHoldRow, BudgetReservationRow, CostEntryRow
from max_agent.db.session import get_session_factory, session_scope
from max_agent.kernel.budget import BudgetService, InsufficientBudgetError
from max_agent.kernel.registry import RegistryService
from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.models.schemas import TaskCreate, WorkerResult


def _setup(db_url: str, *, task_count: int = 1, global_cap: str = "10") -> tuple[str, list]:
    start = datetime.now(timezone.utc) - timedelta(days=1)
    end = start + timedelta(days=30)
    with session_scope(db_url) as session:
        account = RegistryService(session).register_account(
            owner="user", account_key="api", provider_key="openai", billing_mode="api_metered", enabled=True
        )
        tasks = [TaskService(session).create(TaskCreate(goal=f"budget task {i}")).id for i in range(task_count)]
        budgets = BudgetService(session)
        for scope, scope_id, cap in [("global", "all", global_cap), ("provider", "openai", "8")]:
            budgets.create_policy(owner="user", scope=scope, scope_id=scope_id, unit="USD",
                                  period_start=start, period_end=end, ceiling=cap)
        for task_id in tasks:
            budgets.create_policy(owner="user", scope="task", scope_id=task_id, unit="USD",
                                  period_start=start, period_end=end, ceiling="6")
        account_id = account.id
    factory = get_session_factory(db_url)
    scheduler = Scheduler(session_factory=factory, max_concurrency=max(task_count, 1))
    claims = [scheduler.acquire_lease(task_id) for task_id in tasks]
    return account_id, claims


def _reserve(db_url: str, account_id: str, claim, amount: str = "4") -> str:
    with session_scope(db_url) as session:
        row = BudgetService(session).reserve(
            owner="user", account_id=account_id, task_id=claim.task_id,
            attempt_id=claim.attempt_id, fencing_token=claim.fencing_token,
            unit="USD", upper_bound=amount, idempotency_key=claim.attempt_id,
            now=datetime.now(timezone.utc),
        )
        return row.id


def test_reservation_idempotency_and_three_scope_limit(db_url: str) -> None:
    account_id, claims = _setup(db_url, task_count=2, global_cap="5")
    first = _reserve(db_url, account_id, claims[0])
    assert _reserve(db_url, account_id, claims[0]) == first
    with pytest.raises(InsufficientBudgetError, match="global"):
        _reserve(db_url, account_id, claims[1])
    with session_scope(db_url) as session:
        row = session.get(BudgetReservationRow, first)
        assert row.reserved == Decimal("4")
        assert sorted(hold.committed for hold in session.query(BudgetHoldRow).all()) == [Decimal("4")] * 3
        assert session.query(BudgetReservationRow).count() == 1
        with pytest.raises(ValueError, match="conflicts"):
            BudgetService(session).reserve(
                owner="user", account_id=account_id, task_id=claims[0].task_id,
                attempt_id=claims[0].attempt_id, fencing_token=claims[0].fencing_token,
                unit="USD", upper_bound="5", idempotency_key=claims[0].attempt_id,
                now=datetime.now(timezone.utc),
            )


def test_concurrent_reservations_do_not_oversell(db_url: str) -> None:
    account_id, claims = _setup(db_url, task_count=2, global_cap="5")
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def runner(claim) -> None:
        barrier.wait()
        try:
            _reserve(db_url, account_id, claim)
            outcomes.append("reserved")
        except InsufficientBudgetError:
            outcomes.append("denied")

    threads = [threading.Thread(target=runner, args=(claim,)) for claim in claims]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["denied", "reserved"]


def test_measured_settlement_releases_difference_and_records_overrun(db_url: str) -> None:
    account_id, claims = _setup(db_url, task_count=2, global_cap="7")
    first = _reserve(db_url, account_id, claims[0], "4")
    scheduler = Scheduler(session_factory=get_session_factory(db_url), max_concurrency=2)
    scheduler.finalize_lease(claims[0], WorkerResult(
        ok=True, summary="done", outputs={"usage": {
            "source": "api_spend", "marker": "measured", "amount": 1.25, "currency": "USD",
            "billing_mode": "api_metered",
        }},
    ))
    with session_scope(db_url) as session:
        entry = session.query(CostEntryRow).one()
        budgets = BudgetService(session)
        assert budgets.settle_from_entry(first, cost_entry_id=entry.id).actual == Decimal("1.25")
        assert budgets.settle_from_entry(first, cost_entry_id=entry.id).state == "settled"
        assert {hold.committed for hold in session.query(BudgetHoldRow).all()} == {Decimal("1.25")}
    second = _reserve(db_url, account_id, claims[1], "4")
    with session_scope(db_url) as session:
        budgets = BudgetService(session)
        budgets.require_reconciliation(second, reason="provider statement pending")
        assert budgets.reconcile_measured(second, amount="5.5", unit="USD", evidence_ref="statement:123").state == "settled"
        assert {hold.committed for hold in session.query(BudgetHoldRow).filter_by(reservation_id=second)} == {Decimal("5.5")}
        assert budgets.reconcile_measured(second, amount="5.5", unit="USD", evidence_ref="statement:123").actual == Decimal("5.5")


def test_unknown_spend_remains_reserved_until_evidenced(db_url: str) -> None:
    account_id, claims = _setup(db_url)
    reservation_id = _reserve(db_url, account_id, claims[0], "4")
    with session_scope(db_url) as session:
        budgets = BudgetService(session)
        budgets.require_reconciliation(reservation_id, reason="worker disappeared")
        with pytest.raises(ValueError, match="requires reconciliation"):
            budgets.release_uninvoked(reservation_id, evidence_ref="guess")
        assert {hold.committed for hold in session.query(BudgetHoldRow).all()} == {Decimal("4")}
        budgets.reconcile_measured(reservation_id, amount="0", unit="USD", evidence_ref="statement:no-charge")
        assert {hold.committed for hold in session.query(BudgetHoldRow).all()} == {Decimal(0)}


def test_release_only_for_uninvoked_active_attempt(db_url: str) -> None:
    account_id, claims = _setup(db_url)
    reservation_id = _reserve(db_url, account_id, claims[0])
    with session_scope(db_url) as session:
        budgets = BudgetService(session)
        assert budgets.release_uninvoked(reservation_id, evidence_ref="dispatch:not-started").state == "released"
        assert budgets.release_uninvoked(reservation_id, evidence_ref="dispatch:not-started").state == "released"
        assert {hold.committed for hold in session.query(BudgetHoldRow).all()} == {Decimal(0)}
        with pytest.raises(ValueError, match="requires a reconciliation state"):
            budgets.reconcile_measured(reservation_id, amount="1", unit="USD", evidence_ref="statement:late")


def test_missing_unit_policy_fails_closed_and_owner_is_checked(db_url: str) -> None:
    account_id, claims = _setup(db_url)
    with session_scope(db_url) as session:
        budgets = BudgetService(session)
        with pytest.raises(InsufficientBudgetError, match="active global budget"):
            budgets.reserve(
                owner="user", account_id=account_id, task_id=claims[0].task_id,
                attempt_id=claims[0].attempt_id, fencing_token=claims[0].fencing_token,
                unit="credits", upper_bound="1", idempotency_key="credits-only",
                now=datetime.now(timezone.utc),
            )
        with pytest.raises(InsufficientBudgetError, match="owner"):
            budgets.reserve(
                owner="someone-else", account_id=account_id, task_id=claims[0].task_id,
                attempt_id=claims[0].attempt_id, fencing_token=claims[0].fencing_token,
                unit="USD", upper_bound="1", idempotency_key="other-owner",
                now=datetime.now(timezone.utc),
            )
        assert session.query(BudgetReservationRow).count() == 0
