"""Preview is scoped and side-effect free; dispatch remains closed by default."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from max_agent.api.app import create_app
from max_agent.db.models import BudgetReservationRow, TaskAttemptRow, TaskEventRow, TaskRow
from max_agent.db.session import session_scope
from max_agent.kernel.budget import BudgetService
from max_agent.kernel.registry import RegistryService
from max_agent.kernel.routing import RoutePolicy, RouteService
from max_agent.kernel.lease import AttemptClaim, claim_in_session
from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.models.enums import WorkerKind
from max_agent.models.schemas import ScheduleRequest, TaskCreate, WorkerResult


def test_deterministic_candidate_order_and_gates(db_url: str) -> None:
    at = datetime.now(timezone.utc)
    with session_scope(db_url) as session:
        registry = RegistryService(session)
        account = registry.register_account(owner="user", account_key="primary", provider_key="openai",
                                            billing_mode="api_metered", credential_ref="env:OPENAI_API_KEY",
                                            enabled=True)
        a = registry.register_model(provider_key="openai", model_key="model-a", capabilities=["code"],
                                    supported_efforts=["high"], enabled=True)
        b = registry.register_model(provider_key="openai", model_key="model-b", capabilities=["code"],
                                    supported_efforts=["high"], enabled=True)
        for model in (a, b):
            registry.add_price(account_id=account.id, model_id=model.id, effective_from=at - timedelta(days=2),
                               pricing_unit="tokens", currency="USD", source="https://example.org/pricing",
                               input_rate="0.1")
        task_id = TaskService(session).create(TaskCreate(goal="route", worker_payload={"route": {
            "unit": "USD", "upper_bound": "4", "capabilities": ["code"], "effort": "high",
            "vendor_pin": "codex",
        }})).id
        budgets = BudgetService(session)
        for scope, key in (("global", "all"), ("provider", "openai"), ("task", task_id)):
            budgets.create_policy(owner="user", scope=scope, scope_id=key, unit="USD",
                                  period_start=at - timedelta(days=1), period_end=at + timedelta(days=1), ceiling="5")
        task = session.get(TaskRow, task_id)
        policy = RoutePolicy.model_validate(task.worker_payload["route"])
        router = RouteService(session)
        before = (session.query(TaskEventRow).count(), session.query(TaskAttemptRow).count())
        kwargs = {"task": task, "policy": policy, "at": at, "worker_gates": {"codex": True},
                  "model_binding_gates": {"codex": True}, "spend_cap_gates": {"codex": True}}
        first, again = router.preview(**kwargs), router.preview(**kwargs)
        assert first == again
        assert [c.model for c in first.candidates] == ["model-a", "model-b"]
        assert first.selected.model == "model-a"
        assert before == (session.query(TaskEventRow).count(), session.query(TaskAttemptRow).count())
        assert session.query(BudgetReservationRow).count() == 0
        closed = router.preview(task=task, policy=policy, at=at)
        assert closed.selected is None
        assert "model_binding_unavailable" in closed.candidates[0].reason_codes
        assert "spend_ceiling_unenforced" in closed.candidates[0].reason_codes
        no_cap = router.preview(task=task, policy=policy, at=at,
                                worker_gates={"codex": True}, model_binding_gates={"codex": True})
        assert no_cap.selected is None
        assert no_cap.candidates[0].reason_codes == ["spend_ceiling_unenforced"]


def test_http_preview_requires_auth_and_task_owner(db_url: str) -> None:
    with session_scope(db_url) as session:
        owner = TaskService(session).create(TaskCreate(goal="owned", worker_payload={
            "route": {"unit": "USD", "upper_bound": "1"}
        }), created_by="user").id
        other = TaskService(session).create(TaskCreate(goal="other", worker_payload={
            "route": {"unit": "USD", "upper_bound": "1"}
        }), created_by="other").id
    client = TestClient(create_app())
    headers = {"Authorization": "Bearer test-api-token"}
    assert client.post("/v1/routes/preview", json={"task_id": owner}).status_code == 401
    assert client.post("/v1/routes/preview", json={"task_id": other}, headers=headers).status_code == 403
    with session_scope(db_url) as session:
        before = (session.query(TaskEventRow).count(), session.query(TaskAttemptRow).count())
    response = client.post("/v1/routes/preview", json={"task_id": owner}, headers=headers)
    assert response.status_code == 200
    assert response.json()["selected"] is None
    with session_scope(db_url) as session:
        assert before == (session.query(TaskEventRow).count(), session.query(TaskAttemptRow).count())


def test_price_and_budget_do_not_override_wrong_credential_binding(db_url: str) -> None:
    at = datetime.now(timezone.utc)
    with session_scope(db_url) as session:
        account = RegistryService(session).register_account(
            owner="user", account_key="wrong", provider_key="openai", billing_mode="subscription",
            credential_ref="env:ANTHROPIC_API_KEY", enabled=True,
        )
        RegistryService(session).register_model(provider_key="openai", model_key="model-a",
                                                 capabilities=["code"], enabled=True)
        task_id = TaskService(session).create(TaskCreate(goal="wrong credential", worker_payload={
            "route": {"unit": "USD", "upper_bound": "1"}
        })).id
        task = session.get(TaskRow, task_id)
        candidate = RouteService(session).preview(
            task=task, policy=RoutePolicy(unit="USD", upper_bound=1), at=at,
            worker_gates={"codex": True}, model_binding_gates={"codex": True},
            spend_cap_gates={"codex": True},
        ).candidates[0]
        assert candidate.account_id == account.id
        assert "credential_binding_missing" in candidate.reason_codes
        assert "billing_mode_unsupported" in candidate.reason_codes
        assert not candidate.dispatchable


def test_shared_api_key_reference_cannot_choose_one_account(db_url: str) -> None:
    at = datetime.now(timezone.utc)
    with session_scope(db_url) as session:
        registry = RegistryService(session)
        for key in ("first", "second"):
            registry.register_account(owner="user", account_key=key, provider_key="openai",
                                      billing_mode="api_metered", credential_ref="env:OPENAI_API_KEY",
                                      enabled=True)
        registry.register_model(provider_key="openai", model_key="model-a", capabilities=["code"], enabled=True)
        task_id = TaskService(session).create(TaskCreate(goal="ambiguous account", worker_payload={
            "route": {"unit": "USD", "upper_bound": "1"}
        })).id
        task = session.get(TaskRow, task_id)
        preview = RouteService(session).preview(
            task=task, policy=RoutePolicy(unit="USD", upper_bound=1), at=at,
            worker_gates={"codex": True}, model_binding_gates={"codex": True},
            spend_cap_gates={"codex": True},
        )
        assert preview.selected is None
        assert len(preview.candidates) == 2
        assert all("credential_binding_ambiguous" in c.reason_codes for c in preview.candidates)


def test_route_opted_task_cannot_fall_through_to_legacy_dispatch(db_url: str) -> None:
    with session_scope(db_url) as session:
        task_id = TaskService(session).create(TaskCreate(goal="paid route", worker_payload={
            "route": {"unit": "USD", "upper_bound": "1"}, "mode": "success",
        })).id
    result = Scheduler().run_one(task_id)
    assert result.status.value in {"pending", "ready"}
    with session_scope(db_url) as session:
        row = session.get(TaskRow, task_id)
        assert row.fencing_token == 0
        assert session.query(TaskAttemptRow).filter_by(task_id=task_id).count() == 0
        assert session.query(TaskEventRow).filter_by(task_id=task_id, event_type="route_rejected").count() == 1


def _routed_claim(db_url: str, *, now: datetime | None = None) -> AttemptClaim:
    instant = now or datetime.now(timezone.utc)
    with session_scope(db_url) as session:
        registry = RegistryService(session)
        account = registry.register_account(owner="user", account_key="metered", provider_key="openai",
                                            billing_mode="api_metered", credential_ref="env:OPENAI_API_KEY",
                                            enabled=True)
        model = registry.register_model(provider_key="openai", model_key="model-a", capabilities=["code"],
                                        enabled=True)
        registry.add_price(account_id=account.id, model_id=model.id,
                           effective_from=instant - timedelta(days=1), pricing_unit="tokens", currency="USD",
                           source="https://example.org/pricing", input_rate="0.1")
        task_id = TaskService(session).create(TaskCreate(
            goal="route claim", worker_kind=WorkerKind.CLI,
            worker_payload={"route": {"unit": "USD", "upper_bound": "4", "capabilities": ["code"]}},
        )).id
        budgets = BudgetService(session)
        for scope, key in (("global", "all"), ("provider", "openai"), ("task", task_id)):
            budgets.create_policy(owner="user", scope=scope, scope_id=key, unit="USD",
                                  period_start=instant - timedelta(hours=1), period_end=instant + timedelta(days=1),
                                  ceiling="5")
        claim = claim_in_session(
            session, task_id=task_id, worker_id="test", now=instant, ttl_seconds=60,
            req=ScheduleRequest(), automatic=False, backoff_base_seconds=1,
            backoff_cap_seconds=10, route_worker_gates={"codex": True},
            route_model_gates={"codex": True}, route_spend_cap_gates={"codex": True},
        )
        assert isinstance(claim, AttemptClaim)
        assert claim.payload["vendor"] == "codex"
        assert claim.payload["model"] == "model-a"
        assert claim.payload["credential_env"] == "OPENAI_API_KEY"
        assert claim.payload["require_real_worker"] is True
        return claim


def test_route_attempt_reservation_is_atomic_and_measured_settles(db_url: str) -> None:
    from decimal import Decimal
    from max_agent.db.models import BudgetHoldRow, CostEntryRow, RouteDecisionRow

    claim = _routed_claim(db_url)
    with session_scope(db_url) as session:
        decision = session.query(RouteDecisionRow).filter_by(attempt_id=claim.attempt_id).one()
        assert decision.fencing_token == claim.fencing_token
        assert decision.policy_version == "route.v1"
        assert decision.policy_snapshot["capabilities"] == ["code"]
        assert session.query(BudgetReservationRow).filter_by(id=decision.reservation_id).one().reserved == Decimal("4")
        assert {h.committed for h in session.query(BudgetHoldRow).all()} == {Decimal("4")}
        route_link = next(link for link in TaskService(session).trace(claim.task_id).chain if link.kind == "route")
        assert route_link.detail["model"] == "model-a"
        assert route_link.detail["reservation_id"] == decision.reservation_id
        assert "credential" not in str(route_link.detail)
        assert session.query(TaskEventRow).filter_by(task_id=claim.task_id, event_type="route_selected").count() == 1
    Scheduler().finalize_lease(claim, WorkerResult(
        ok=True, summary="done", outputs={
            "real_cli_invoked": True, "model": "model-a", "provider": "openai",
            "usage": {"source": "api_spend", "marker": "measured", "amount": 1.25,
                      "currency": "USD", "model": "model-a", "provider": "openai"},
        },
    ))
    with session_scope(db_url) as session:
        reservation = session.query(BudgetReservationRow).one()
        assert reservation.state == "settled" and reservation.actual == Decimal("1.25")
        assert {h.committed for h in session.query(BudgetHoldRow).all()} == {Decimal("1.25")}
        assert session.query(CostEntryRow).one().price_version_id is not None


def test_route_attempt_unknown_spend_stays_held(db_url: str) -> None:
    from decimal import Decimal
    from max_agent.db.models import BudgetHoldRow

    claim = _routed_claim(db_url)
    Scheduler().finalize_lease(claim, WorkerResult(
        ok=True, summary="done", outputs={"real_cli_invoked": True,
                                         "model": "model-a", "provider": "openai"},
    ))
    with session_scope(db_url) as session:
        reservation = session.query(BudgetReservationRow).one()
        assert reservation.state == "reconcile_required"
        assert {h.committed for h in session.query(BudgetHoldRow).all()} == {Decimal("4")}


def test_route_recovery_keeps_unknown_spend_reserved(db_url: str) -> None:
    from decimal import Decimal
    from max_agent.db.models import BudgetHoldRow

    claim = _routed_claim(db_url, now=datetime.now(timezone.utc) - timedelta(minutes=2))
    assert Scheduler().recover_expired()
    with session_scope(db_url) as session:
        reservation = session.query(BudgetReservationRow).one()
        assert reservation.state == "reconcile_required"
        assert {h.committed for h in session.query(BudgetHoldRow).all()} == {Decimal("4")}


def test_route_mismatched_model_is_not_accepted_or_released(db_url: str) -> None:
    from decimal import Decimal
    from max_agent.db.models import BudgetHoldRow

    claim = _routed_claim(db_url)
    result = Scheduler().finalize_lease(claim, WorkerResult(
        ok=True, summary="done", outputs={
            "real_cli_invoked": True, "model": "different-model", "provider": "openai",
            "usage": {"source": "api_spend", "marker": "measured", "amount": 1.25,
                      "currency": "USD", "model": "different-model", "provider": "openai"},
        },
    ))
    assert result.status.value != "succeeded"
    with session_scope(db_url) as session:
        assert session.query(BudgetReservationRow).one().state == "reconcile_required"
        assert {h.committed for h in session.query(BudgetHoldRow).all()} == {Decimal("4")}


def test_migrated_route_decision_rejects_update(tmp_path, monkeypatch) -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    import pytest
    from max_agent.db.session import reset_engine

    url = f"sqlite:///{tmp_path / 'route-migration.db'}"
    monkeypatch.setenv("MAX_AGENT_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    reset_engine()
    _routed_claim(url)
    with pytest.raises(DBAPIError, match="immutable"):
        with session_scope(url) as session:
            session.execute(text("UPDATE route_decisions SET model_key = 'tampered'"))
    command.downgrade(Config("alembic.ini"), "c77b1f2e8a90")
    reset_engine()
