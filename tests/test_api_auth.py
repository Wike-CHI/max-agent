from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from max_agent.api.app import create_app
from max_agent.db.session import session_scope
from max_agent.kernel.service import TaskService


@pytest.fixture()
def client(db_url: str, monkeypatch: pytest.MonkeyPatch):
    """默认客户端不带任何凭据，每个请求都必须显式给出令牌。"""
    monkeypatch.setenv("MAX_AGENT_API_ACTOR", "alice")
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture()
def create_task(auth_header):
    """创建任务必须使用独立的 create 令牌，不能与审批令牌复用。"""

    def _make(client: TestClient, *, key: str = "auth-task", tags: list[str] | None = None) -> dict:
        response = client.post(
            "/v1/tasks",
            json={
                "goal": "protected task",
                "acceptance_criteria": ["worker_ok"],
                "worker_payload": {"mode": "success"},
                "idempotency_key": key,
                "tags": tags or [],
            },
            headers=auth_header("test-create"),
        )
        assert response.status_code == 200
        return response.json()

    return _make


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

    # 旧的 MAX_AGENT_API_TOKEN 已不再授予全权，夹具里可能已经删除过它。
    monkeypatch.delenv("MAX_AGENT_API_TOKEN", raising=False)
    assert client.get("/v1/tasks").status_code == 401


def test_approval_actor_comes_from_principal_and_forgery_is_rejected(
    client: TestClient, create_task, auth_header
) -> None:
    task = create_task(client, tags=["publish"])
    task_id = task["id"]
    assert task["created_by"] == "alice"
    assert task["status"] == "waiting_approval"

    approve_headers = auth_header("test-approve")

    forged = client.post(
        f"/v1/tasks/{task_id}/approve", json={"actor": "mallory"}, headers=approve_headers
    )
    assert forged.status_code == 403
    fetched = client.get(f"/v1/tasks/{task_id}", headers=auth_header("test-read"))
    assert fetched.json()["approved_by"] is None

    approved = client.post(
        f"/v1/tasks/{task_id}/approve", json={"note": "allowed"}, headers=approve_headers
    )
    assert approved.status_code == 200
    assert approved.json()["task"]["approved_by"] == "alice"

    forged_reject = client.post(
        f"/v1/tasks/{task_id}/reject", json={"actor": "mallory"}, headers=approve_headers
    )
    assert forged_reject.status_code == 403
    rejected = client.post(
        f"/v1/tasks/{task_id}/reject", json={"actor": "alice"}, headers=approve_headers
    )
    assert rejected.status_code == 200
    assert rejected.json()["task"]["approval_decision"] == "rejected"


def test_other_principal_cannot_read_or_mutate_task(
    client: TestClient,
    create_task,
    auth_header,
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = create_task(client, tags=["publish"])
    task_id = task["id"]
    monkeypatch.setenv("MAX_AGENT_API_ACTOR", "bob")

    assert client.get("/v1/tasks", headers=auth_header("test-read")).json() == []
    for method, path, headers in [
        ("get", f"/v1/tasks/{task_id}", auth_header("test-read")),
        ("get", f"/v1/tasks/{task_id}/status", auth_header("test-read")),
        ("get", f"/v1/tasks/{task_id}/events", auth_header("test-read")),
        ("get", f"/v1/tasks/{task_id}/trace", auth_header("test-read")),
        ("post", f"/v1/tasks/{task_id}/schedule", auth_header("test-execute-inline")),
        ("post", f"/v1/tasks/{task_id}/cancel", auth_header("test-cancel")),
        ("post", f"/v1/tasks/{task_id}/approve", auth_header("test-approve")),
        ("post", f"/v1/tasks/{task_id}/reject", auth_header("test-approve")),
    ]:
        response = client.request(
            method, path, json={} if method == "post" else None, headers=headers
        )
        assert response.status_code == 403

    replay = client.post(
        "/v1/tasks",
        json={"goal": "forged replay", "idempotency_key": "auth-task"},
        headers=auth_header("test-create"),
    )
    assert replay.status_code == 403
    with session_scope(db_url) as session:
        events = TaskService(session).list_events(task_id)
    assert all(event.event_type.value != "idempotent_hit" for event in events)


def test_webhook_task_is_owned_by_configured_api_actor(
    client: TestClient, auth_header, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Webhook create must store the same principal require_principal resolves."""
    monkeypatch.setenv("MAX_AGENT_API_ACTOR", "  alice  ")
    monkeypatch.setenv("MAX_AGENT_WEBHOOK_SECRET", "unit-test-secret")
    body = json.dumps(
        {
            "idempotency_key": "webhook-owned-by-alice",
            "goal": "webhook mock",
            "acceptance_criteria": ["worker_ok"],
            "worker_kind": "mock",
            "worker_payload": {"mode": "success"},
        }
    ).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(b"unit-test-secret", stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    created = client.post(
        "/v1/inbound/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Max-Timestamp": stamp,
            "X-Max-Signature": signature,
            "X-Max-Delivery-Id": "delivery-alice",
        },
    )
    assert created.status_code == 200
    payload = created.json()
    task_id = payload["task_id"]
    assert payload["task"]["created_by"] == "alice"

    listed = client.get("/v1/tasks", headers=auth_header("test-read"))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [task_id]
    assert listed.json()[0]["created_by"] == "alice"

    fetched = client.get(f"/v1/tasks/{task_id}", headers=auth_header("test-read"))
    assert fetched.status_code == 200
    assert fetched.json()["created_by"] == "alice"

    monkeypatch.setenv("MAX_AGENT_API_ACTOR", "bob")
    assert client.get("/v1/tasks", headers=auth_header("test-read")).json() == []
    denied = client.get(f"/v1/tasks/{task_id}", headers=auth_header("test-read"))
    assert denied.status_code == 403


def test_queue_only_runs_tasks_owned_by_principal(
    client: TestClient, create_task, auth_header, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = create_task(client, key="alice-task")
    monkeypatch.setenv("MAX_AGENT_API_ACTOR", "bob")
    bob = create_task(client, key="bob-task")

    ran = client.post("/v1/queue/run", headers=auth_header("test-execute-inline"))
    assert ran.status_code == 200
    assert [task["id"] for task in ran.json()] == [bob["id"]]
    monkeypatch.setenv("MAX_AGENT_API_ACTOR", "alice")
    fetched = client.get(f"/v1/tasks/{alice['id']}", headers=auth_header("test-read"))
    assert fetched.json()["status"] == "ready"


def test_legacy_env_token_returns_401(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """旧的 MAX_AGENT_API_TOKEN 不再是可用凭据。"""
    monkeypatch.setenv("MAX_AGENT_API_TOKEN", "legacy-token")
    response = client.get("/v1/tasks", headers={"Authorization": "Bearer legacy-token"})
    assert response.status_code == 401


def test_whoami_needs_no_read_scope(client: TestClient, auth_header) -> None:
    """whoami 只要求令牌本身有效，不需要 read 作用域。"""
    response = client.get("/v1/auth/whoami", headers=auth_header("test-create"))
    assert response.status_code == 200
    body = response.json()

    token_id = body.get("token_id")
    if token_id is None:
        token_id = body.get("token", {}).get("token_id")
    assert token_id == "test-create"

    principal = body.get("principal")
    if isinstance(principal, dict):
        principal = principal.get("actor") or principal.get("name") or principal.get("id")
    assert principal == os.environ.get("MAX_AGENT_API_ACTOR", "tester")

    scopes = body.get("scopes")
    if scopes is None:
        scopes = body.get("token", {}).get("scopes")
    assert list(scopes) == ["create"]

    assert "secret" not in body
    assert "secret_sha256" not in body
    assert "test-create-secret" not in response.text


def test_whoami_rejects_unknown_token(client: TestClient) -> None:
    response = client.get("/v1/auth/whoami", headers={"Authorization": "Bearer unknown-secret"})
    assert response.status_code == 401


def test_read_token_cannot_post_tasks(client: TestClient, auth_header) -> None:
    """只读令牌不能写；不给只读卡补写作用域来迁就实现。"""
    response = client.post(
        "/v1/tasks",
        json={"goal": "read only attempt", "idempotency_key": "read-only"},
        headers=auth_header("test-read"),
    )
    assert response.status_code == 403
