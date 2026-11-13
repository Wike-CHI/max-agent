from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from max_agent.api.app import create_app
from max_agent.db.models import TaskRow
from max_agent.kernel.dag import wake_satisfied
from max_agent.kernel.service import TaskService
from max_agent.models.enums import TaskStatus
from max_agent.models.schemas import TaskCreate


@pytest.fixture()
def client(db_url: str):
    with TestClient(create_app()) as test_client:
        yield test_client


def _create(client, auth_header, make_task_body, **overrides):
    payload = make_task_body(worker_kind="mock", worker_payload={"mode": "success"}, **overrides)
    resp = client.post("/v1/tasks", json=payload, headers=auth_header("test-create"))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _dispatch_body(task: dict) -> dict:
    return {"expected_version": task["version"], "content_hash": "0" * 64}


def _task_payload(resp) -> dict:
    body = resp.json()
    if isinstance(body, dict) and "task" in body and isinstance(body["task"], dict):
        return body["task"]
    return body


def test_task_read_exposes_awaiting_dispatch_and_version(client, auth_header, make_task_body):
    resp = client.post(
        "/v1/tasks",
        json=make_task_body(worker_kind="mock", worker_payload={"mode": "success"}),
        headers=auth_header("test-create"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["version"], int)
    assert body["awaiting_dispatch"] is False


def test_require_dispatch_task_stays_pending(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body, require_dispatch=True)
    assert task["status"] == "pending"
    assert task["awaiting_dispatch"] is True


def test_default_create_keeps_auto_ready(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body)
    assert task["awaiting_dispatch"] is False
    assert task["status"] == "ready"


def test_dispatch_clears_flag_and_enters_state_machine(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body, require_dispatch=True)
    resp = client.post(
        f"/v1/tasks/{task['id']}/dispatch",
        json=_dispatch_body(task),
        headers=auth_header("test-dispatch"),
    )
    assert resp.status_code == 200, resp.text
    body = _task_payload(resp)
    assert body["awaiting_dispatch"] is False
    assert body["status"] == "ready"
    assert body["version"] == task["version"] + 1


def test_dispatch_is_idempotent(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body, require_dispatch=True)
    payload = _dispatch_body(task)
    first = client.post(
        f"/v1/tasks/{task['id']}/dispatch", json=payload, headers=auth_header("test-dispatch")
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"/v1/tasks/{task['id']}/dispatch", json=payload, headers=auth_header("test-dispatch")
    )
    assert second.status_code == 200, second.text
    first_body = _task_payload(first)
    second_body = _task_payload(second)
    assert second_body["version"] == first_body["version"]
    assert second_body["status"] == first_body["status"]


def test_dispatch_does_not_execute_worker_in_api_process(client, auth_header, make_task_body, monkeypatch):
    import max_agent.kernel.scheduler as scheduler_mod

    calls: list[str] = []
    monkeypatch.setattr(
        scheduler_mod.Scheduler, "run_one", lambda *args, **kwargs: calls.append("run_one")
    )
    monkeypatch.setattr(
        scheduler_mod.Scheduler, "run_queue", lambda *args, **kwargs: calls.append("run_queue")
    )
    task = _create(client, auth_header, make_task_body, require_dispatch=True)
    resp = client.post(
        f"/v1/tasks/{task['id']}/dispatch",
        json=_dispatch_body(task),
        headers=auth_header("test-dispatch"),
    )
    assert resp.status_code == 200, resp.text
    assert calls == []


def test_graph_require_dispatch_applies_to_all_new_nodes(client, auth_header):
    body = {
        "nodes": [
            {"key": "a", "goal": "node a", "worker_kind": "mock", "worker_payload": {"mode": "success"}},
            {"key": "b", "goal": "node b", "worker_kind": "mock", "worker_payload": {"mode": "success"}},
        ],
        "require_dispatch": True,
    }
    resp = client.post("/v1/tasks/graph", json=body, headers=auth_header("test-create"))
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    nodes = payload["tasks"] if isinstance(payload, dict) and "tasks" in payload else payload
    assert nodes
    for node in nodes:
        assert node["awaiting_dispatch"] is True
        assert node["status"] == "pending"


def test_wake_satisfied_does_not_auto_ready_awaiting_dispatch(session):
    service = TaskService(session)
    upstream = service.create(
        TaskCreate(goal="up", worker_kind="mock", worker_payload={"mode": "success"})
    )
    downstream = service.create(
        TaskCreate(
            goal="down",
            worker_kind="mock",
            worker_payload={"mode": "success"},
            dependencies=[upstream.id],
            require_dispatch=True,
        )
    )
    assert downstream.status == TaskStatus.PENDING
    assert downstream.awaiting_dispatch is True
    row = session.get(TaskRow, upstream.id)
    assert row is not None
    row.status = TaskStatus.SUCCEEDED.value
    session.flush()
    wake_satisfied(session, now=datetime.now(timezone.utc))
    session.refresh(session.get(TaskRow, downstream.id))
    again = session.get(TaskRow, downstream.id)
    assert again is not None
    assert again.awaiting_dispatch is True
    assert again.status == TaskStatus.PENDING.value


def test_approve_does_not_ready_undispatched_task(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body, require_dispatch=True, tags=["publish"])
    assert task["status"] == "pending"
    assert task["awaiting_dispatch"] is True
    resp = client.post(
        f"/v1/tasks/{task['id']}/approve",
        json={"note": "must not ready an undispatched task"},
        headers=auth_header("test-approve"),
    )
    assert resp.status_code in (200, 409), resp.text
    fetched = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read"))
    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    assert body["awaiting_dispatch"] is True
    assert body["status"] != "ready"
