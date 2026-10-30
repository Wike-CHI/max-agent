from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from max_agent.api.app import create_app
from max_agent.db.session import session_scope
from max_agent.kernel.service import TaskService

AUTH = {"Authorization": "Bearer test-api-token"}


@pytest.fixture()
def client(db_url: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAX_AGENT_API_ACTOR", "alice")
    with TestClient(create_app(), headers=AUTH) as test_client:
        yield test_client


def _create(client: TestClient, *, key: str = "auth-task", tags: list[str] | None = None) -> dict:
    response = client.post(
        "/v1/tasks",
        json={
            "goal": "protected task",
            "acceptance_criteria": ["worker_ok"],
            "worker_payload": {"mode": "success"},
            "idempotency_key": key,
            "tags": tags or [],
        },
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/v1/tasks", {"goal": "no credentials"}),
        ("post", "/v1/tasks/graph", {"nodes": [{"key": "a", "goal": "no credentials"}]}),
        ("get", "/v1/tasks", None),
        ("get", "/v1/tasks/missing", None),
        ("get", "/v1/tasks/missing/status", None),
        ("get", "/v1/tasks/missing/events", None),
        ("get", "/v1/tasks/missing/trace", None),
        ("post", "/v1/tasks/missing/schedule", {}),
        ("post", "/v1/tasks/missing/cancel", None),
        ("post", "/v1/tasks/missing/approve", {}),
        ("post", "/v1/tasks/missing/reject", {}),
        ("post", "/v1/queue/run", None),
        ("get", "/v1/telemetry", None),
        ("get", "/v1/metrics", None),
        ("post", "/v1/aer/retry", None),
    ],
)
def test_protected_routes_require_bearer_token(
    client: TestClient, method: str, path: str, body: dict | None
) -> None:
    response = client.request(method, path, json=body, headers={"Authorization": ""})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_health_ready_are_anonymous_and_invalid_or_unconfigured_tokens_fail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.get("/v1/health", headers={"Authorization": ""}).status_code == 200
    assert client.get("/v1/ready", headers={"Authorization": ""}).status_code == 200
    assert client.get("/v1/tasks", headers={"Authorization": "Bearer wrong"}).status_code == 401

    monkeypatch.delenv("MAX_AGENT_API_TOKEN")
    assert client.get("/v1/tasks").status_code == 401


def test_approval_actor_comes_from_principal_and_forgery_is_rejected(client: TestClient) -> None:
    task = _create(client, tags=["publish"])
    task_id = task["id"]
    assert task["created_by"] == "alice"
    assert task["status"] == "waiting_approval"

    forged = client.post(f"/v1/tasks/{task_id}/approve", json={"actor": "mallory"})
    assert forged.status_code == 403
    assert client.get(f"/v1/tasks/{task_id}").json()["approved_by"] is None

    approved = client.post(f"/v1/tasks/{task_id}/approve", json={"note": "allowed"})
    assert approved.status_code == 200
    assert approved.json()["task"]["approved_by"] == "alice"

    forged_reject = client.post(f"/v1/tasks/{task_id}/reject", json={"actor": "mallory"})
    assert forged_reject.status_code == 403
    rejected = client.post(f"/v1/tasks/{task_id}/reject", json={"actor": "alice"})
    assert rejected.status_code == 200
    assert rejected.json()["task"]["approval_decision"] == "rejected"


def test_other_principal_cannot_read_or_mutate_task(
    client: TestClient, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _create(client, tags=["publish"])
    task_id = task["id"]
    monkeypatch.setenv("MAX_AGENT_API_ACTOR", "bob")

    assert client.get("/v1/tasks").json() == []
    for method, path in [
        ("get", f"/v1/tasks/{task_id}"),
        ("get", f"/v1/tasks/{task_id}/status"),
        ("get", f"/v1/tasks/{task_id}/events"),
        ("get", f"/v1/tasks/{task_id}/trace"),
        ("post", f"/v1/tasks/{task_id}/schedule"),
        ("post", f"/v1/tasks/{task_id}/cancel"),
        ("post", f"/v1/tasks/{task_id}/approve"),
        ("post", f"/v1/tasks/{task_id}/reject"),
    ]:
        response = client.request(method, path, json={} if method == "post" else None)
        assert response.status_code == 403

    replay = client.post(
        "/v1/tasks",
        json={"goal": "forged replay", "idempotency_key": "auth-task"},
    )
    assert replay.status_code == 403
    with session_scope(db_url) as session:
        events = TaskService(session).list_events(task_id)
    assert all(event.event_type.value != "idempotent_hit" for event in events)


def test_queue_only_runs_tasks_owned_by_principal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = _create(client, key="alice-task")
    monkeypatch.setenv("MAX_AGENT_API_ACTOR", "bob")
    bob = _create(client, key="bob-task")

    ran = client.post("/v1/queue/run")
    assert ran.status_code == 200
    assert [task["id"] for task in ran.json()] == [bob["id"]]
    monkeypatch.setenv("MAX_AGENT_API_ACTOR", "alice")
    assert client.get(f"/v1/tasks/{alice['id']}").json()["status"] == "ready"
