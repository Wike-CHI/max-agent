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
from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.models.schemas import TaskCreate


def test_deterministic_candidate_order_and_gates(db_url: str) -> None:
    at = datetime.now(timezone.utc)
    with session_scope(db_url) as session:
        registry = RegistryService(session)
        account = registry.register_account(owner="user", account_key="primary", provider_key="openai",
                                            billing_mode="api_metered", enabled=True)
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
                  "model_binding_gates": {"codex": True}}
        first, again = router.preview(**kwargs), router.preview(**kwargs)
        assert first == again
        assert [c.model for c in first.candidates] == ["model-a", "model-b"]
        assert first.selected.model == "model-a"
        assert before == (session.query(TaskEventRow).count(), session.query(TaskAttemptRow).count())
        assert session.query(BudgetReservationRow).count() == 0
        closed = router.preview(task=task, policy=policy, at=at)
        assert closed.selected is None
        assert "model_binding_unavailable" in closed.candidates[0].reason_codes


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
