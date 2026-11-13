from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from max_agent.api.app import create_app
from max_agent.db.models import TaskRow, utcnow
from max_agent.kernel.errors import LeaseHeldError
from max_agent.kernel.lease import claim_in_session
from max_agent.kernel.service import TaskService
from max_agent.models.enums import TaskStatus
from max_agent.models.schemas import ScheduleRequest, TaskCreate


@pytest.fixture()
def client(db_url: str):
    with TestClient(create_app()) as test_client:
        yield test_client


def _create_gated(client, auth_header, make_task_body, **overrides):
    payload = make_task_body(
        worker_kind="mock",
        worker_payload={"mode": "success"},
        require_dispatch=True,
        **overrides,
    )
    resp = client.post("/v1/tasks", json=payload, headers=auth_header("test-create"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["awaiting_dispatch"] is True
    assert body["status"] == "pending"
    return body


def _claim(session, task_id: str, *, automatic: bool):
    return claim_in_session(
        session,
        task_id=task_id,
        worker_id="tester",
        now=utcnow(),
        ttl_seconds=30,
        req=ScheduleRequest(),
        automatic=automatic,
        backoff_base_seconds=1.0,
        backoff_cap_seconds=2.0,
    )


def test_claim_in_session_refuses_awaiting_dispatch(session):
    service = TaskService(session)
    task = service.create(
        TaskCreate(
            goal="gated claim",
            worker_kind="mock",
            worker_payload={"mode": "success"},
            require_dispatch=True,
        )
    )
    assert task.awaiting_dispatch is True
    assert task.status == TaskStatus.PENDING
    with pytest.raises(ValueError):
        _claim(session, task.id, automatic=False)
    row = session.get(TaskRow, task.id)
    assert row is not None
    assert row.awaiting_dispatch is True
    assert row.status == TaskStatus.PENDING.value
    assert row.leased_by is None
    assert int(row.attempts or 0) == 0


def test_claim_in_session_refuses_even_if_status_is_ready_or_retry_wait(session):
    service = TaskService(session)
    task = service.create(
        TaskCreate(
            goal="gated ready inconsistency",
            worker_kind="mock",
            worker_payload={"mode": "success"},
            require_dispatch=True,
        )
    )
    for status in (TaskStatus.READY, TaskStatus.RETRY_WAIT):
        row = session.get(TaskRow, task.id)
        assert row is not None
        row.status = status.value
        session.flush()
        with pytest.raises(ValueError):
            _claim(session, task.id, automatic=True)
        again = session.get(TaskRow, task.id)
        assert again is not None
        assert again.awaiting_dispatch is True
        assert again.status not in {TaskStatus.LEASED.value, TaskStatus.RUNNING.value}
        assert again.leased_by is None
        assert int(again.attempts or 0) == 0


def test_http_schedule_with_execute_inline_cannot_bypass_dispatch_gate(client, auth_header, make_task_body):
    task = _create_gated(client, auth_header, make_task_body)
    resp = client.post(
        f"/v1/tasks/{task['id']}/schedule",
        json={},
        headers=auth_header("test-execute-inline"),
    )
    assert resp.status_code == 409, resp.text
    fetched = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read"))
    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    assert body["awaiting_dispatch"] is True
    assert body["status"] == "pending"
    assert body["status"] not in {"ready", "leased", "running", "succeeded"}


def test_http_queue_run_does_not_pick_awaiting_dispatch_tasks(client, auth_header, make_task_body):
    task = _create_gated(client, auth_header, make_task_body)
    resp = client.post("/v1/queue/run", params={"limit": 50}, headers=auth_header("test-execute-inline"))
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    rows = payload if isinstance(payload, list) else payload.get("tasks") or payload.get("items") or []
    ids = [item["id"] for item in rows]
    assert task["id"] not in ids
    fetched = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read"))
    body = fetched.json()
    assert body["awaiting_dispatch"] is True
    assert body["status"] == "pending"


def test_dispatch_lease_held_error_is_http_409(client, auth_header, make_task_body, monkeypatch):
    task = _create_gated(client, auth_header, make_task_body)

    def boom(self, task_id: str):
        raise LeaseHeldError(f"concurrent update on task {task_id}")

    monkeypatch.setattr("max_agent.kernel.service.TaskService.dispatch", boom)
    resp = client.post(
        f"/v1/tasks/{task['id']}/dispatch",
        json={"expected_version": task["version"], "content_hash": "0" * 64},
        headers=auth_header("test-dispatch"),
    )
    assert resp.status_code == 409, resp.text


def test_dispatch_other_exceptions_are_not_swallowed(client, auth_header, make_task_body, monkeypatch, db_url):
    """raise_server_exceptions=False so an unhandled error is observed as HTTP 500, not a client-side re-raise."""
    task = _create_gated(client, auth_header, make_task_body)

    def boom(self, task_id: str):
        raise RuntimeError("unexpected dispatch failure")

    monkeypatch.setattr("max_agent.kernel.service.TaskService.dispatch", boom)
    with TestClient(create_app(), raise_server_exceptions=False) as raw:
        resp = raw.post(
            f"/v1/tasks/{task['id']}/dispatch",
            json={"expected_version": task["version"], "content_hash": "0" * 64},
            headers=auth_header("test-dispatch"),
        )
    assert resp.status_code == 500, resp.text
    assert resp.status_code != 409


def test_execute_inline_token_cannot_dispatch(client, auth_header, make_task_body):
    task = _create_gated(client, auth_header, make_task_body)
    resp = client.post(
        f"/v1/tasks/{task['id']}/dispatch",
        json={"expected_version": task["version"], "content_hash": "0" * 64},
        headers=auth_header("test-execute-inline"),
    )
    assert resp.status_code == 403, resp.text


def test_dispatch_token_cannot_schedule(client, auth_header, make_task_body):
    task = _create_gated(client, auth_header, make_task_body)
    resp = client.post(
        f"/v1/tasks/{task['id']}/schedule",
        json={},
        headers=auth_header("test-dispatch"),
    )
    assert resp.status_code == 403, resp.text
