from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from max_agent.api.app import create_app
from max_agent.kernel.service import HASH_FIELDS, canonical_json


@pytest.fixture()
def client(db_url: str):
    with TestClient(create_app()) as test_client:
        yield test_client


def _create(client, auth_header, make_task_body, **overrides):
    overrides.setdefault("worker_kind", "mock")
    overrides.setdefault("worker_payload", {"mode": "success"})
    resp = client.post(
        "/v1/tasks",
        json=make_task_body(**overrides),
        headers=auth_header("test-create"),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _waiting_approval(client, auth_header, make_task_body, **overrides):
    overrides.setdefault("tags", ["payment"])
    task = _create(client, auth_header, make_task_body, **overrides)
    assert task["status"] == "waiting_approval"
    assert "content_hash" in task and len(task["content_hash"]) == 64
    return task


def _task_payload(resp) -> dict:
    body = resp.json()
    if isinstance(body, dict) and "task" in body and isinstance(body["task"], dict):
        return body["task"]
    return body


def _write_body(task: dict, **extra) -> dict:
    body = {"expected_version": task["version"], "content_hash": task["content_hash"]}
    body.update(extra)
    return body


def test_hash_fields_are_exactly_thirteen_and_exclude_version():
    assert len(HASH_FIELDS) == 13
    assert "version" not in HASH_FIELDS
    assert set(HASH_FIELDS) == {
        "goal",
        "acceptance_criteria",
        "worker_kind",
        "worker_payload",
        "tags",
        "dependencies",
        "has_side_effects",
        "issue_ref",
        "priority",
        "timeout_seconds",
        "max_attempts",
        "max_cost_units",
        "allow_auto_retry_side_effects",
    }


def test_canonical_json_is_sorted_and_compact():
    assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_content_hash_is_lowercase_sha256_hex(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body)
    assert len(task["content_hash"]) == 64
    assert task["content_hash"] == task["content_hash"].lower()
    int(task["content_hash"], 16)


def test_content_hash_is_stable_across_identical_tasks(client, auth_header, make_task_body):
    a = _create(client, auth_header, make_task_body, goal="same goal")
    b = _create(client, auth_header, make_task_body, goal="same goal")
    assert a["content_hash"] == b["content_hash"]
    assert a["version"] == b["version"]


def test_content_hash_changes_with_content(client, auth_header, make_task_body):
    a = _create(client, auth_header, make_task_body, goal="goal one")
    b = _create(client, auth_header, make_task_body, goal="goal two")
    assert a["content_hash"] != b["content_hash"]


def test_content_hash_excludes_identity_and_status(client, auth_header, make_task_body):
    a = _create(client, auth_header, make_task_body, goal="same content")
    b = _create(client, auth_header, make_task_body, goal="same content")
    assert a["id"] != b["id"]
    assert a["content_hash"] == b["content_hash"]
    fetched = client.get(f"/v1/tasks/{a['id']}", headers=auth_header("test-read"))
    assert fetched.status_code == 200
    assert fetched.json()["content_hash"] == a["content_hash"]


def test_approve_with_incomplete_sensitive_tag_scope_returns_409(client, auth_header, make_task_body):
    task = _waiting_approval(client, auth_header, make_task_body)
    resp = client.post(
        f"/v1/tasks/{task['id']}/approve",
        json=_write_body(task, scope=["delete"]),
        headers=auth_header("test-approve"),
    )
    assert resp.status_code == 409
    again = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read")).json()
    assert again["status"] == "waiting_approval"
    assert again["version"] == task["version"]


def test_approve_with_full_sensitive_tag_scope_succeeds(client, auth_header, make_task_body):
    task = _waiting_approval(client, auth_header, make_task_body)
    resp = client.post(
        f"/v1/tasks/{task['id']}/approve",
        json=_write_body(task, scope=["payment"]),
        headers=auth_header("test-approve"),
    )
    assert resp.status_code == 200, resp.text
    body = _task_payload(resp)
    assert body["status"] != "waiting_approval"
    assert body["version"] == task["version"] + 1
    assert body["content_hash"] == task["content_hash"]


def test_approve_is_idempotent(client, auth_header, make_task_body):
    task = _waiting_approval(client, auth_header, make_task_body)
    payload = _write_body(task, scope=["payment"])
    first = client.post(
        f"/v1/tasks/{task['id']}/approve", json=payload, headers=auth_header("test-approve")
    )
    assert first.status_code == 200
    second = client.post(
        f"/v1/tasks/{task['id']}/approve", json=payload, headers=auth_header("test-approve")
    )
    assert second.status_code == 200
    assert _task_payload(second)["version"] == _task_payload(first)["version"]


def test_approve_with_stale_version_returns_409(client, auth_header, make_task_body):
    task = _waiting_approval(client, auth_header, make_task_body)
    resp = client.post(
        f"/v1/tasks/{task['id']}/approve",
        json=_write_body(task, scope=["payment"], expected_version=task["version"] + 99),
        headers=auth_header("test-approve"),
    )
    assert resp.status_code == 409
    again = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read")).json()
    assert again["status"] == "waiting_approval"
    assert again["version"] == task["version"]


def test_approve_with_wrong_hash_returns_409(client, auth_header, make_task_body):
    task = _waiting_approval(client, auth_header, make_task_body)
    resp = client.post(
        f"/v1/tasks/{task['id']}/approve",
        json=_write_body(task, scope=["payment"], content_hash="0" * 64),
        headers=auth_header("test-approve"),
    )
    assert resp.status_code == 409
    again = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read")).json()
    assert again["version"] == task["version"]


def test_approve_missing_version_or_hash_returns_4xx(client, auth_header, make_task_body):
    task = _waiting_approval(client, auth_header, make_task_body)
    missing = client.post(
        f"/v1/tasks/{task['id']}/approve",
        json={"scope": ["payment"]},
        headers=auth_header("test-approve"),
    )
    assert missing.status_code in (400, 422)
    again = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read")).json()
    assert again["status"] == "waiting_approval"
    assert again["version"] == task["version"]


def test_reject_uses_approve_scope_and_is_idempotent(client, auth_header, make_task_body):
    task = _waiting_approval(client, auth_header, make_task_body)
    payload = _write_body(task)
    first = client.post(
        f"/v1/tasks/{task['id']}/reject", json=payload, headers=auth_header("test-approve")
    )
    assert first.status_code == 200
    second = client.post(
        f"/v1/tasks/{task['id']}/reject", json=payload, headers=auth_header("test-approve")
    )
    assert second.status_code == 200
    assert _task_payload(second)["version"] == _task_payload(first)["version"]


def test_reject_with_dispatch_token_returns_403(client, auth_header, make_task_body):
    task = _waiting_approval(client, auth_header, make_task_body)
    resp = client.post(
        f"/v1/tasks/{task['id']}/reject",
        json=_write_body(task),
        headers=auth_header("test-dispatch"),
    )
    assert resp.status_code == 403


def test_dispatch_with_wrong_hash_or_stale_version_returns_409(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body, require_dispatch=True)
    wrong = client.post(
        f"/v1/tasks/{task['id']}/dispatch",
        json=_write_body(task, content_hash="0" * 64),
        headers=auth_header("test-dispatch"),
    )
    assert wrong.status_code == 409
    stale = client.post(
        f"/v1/tasks/{task['id']}/dispatch",
        json=_write_body(task, expected_version=task["version"] + 99),
        headers=auth_header("test-dispatch"),
    )
    assert stale.status_code == 409
    again = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read")).json()
    assert again["awaiting_dispatch"] is True
    assert again["status"] == "pending"
    assert again["version"] == task["version"]


def test_dispatch_missing_version_or_hash_returns_4xx(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body, require_dispatch=True)
    resp = client.post(
        f"/v1/tasks/{task['id']}/dispatch",
        json={},
        headers=auth_header("test-dispatch"),
    )
    assert resp.status_code in (400, 422)
    again = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read")).json()
    assert again["awaiting_dispatch"] is True
    assert again["version"] == task["version"]


def test_matching_dispatch_is_idempotent_and_does_not_run_worker(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body, require_dispatch=True)
    payload = _write_body(task)
    first = client.post(
        f"/v1/tasks/{task['id']}/dispatch", json=payload, headers=auth_header("test-dispatch")
    )
    assert first.status_code == 200, first.text
    first_body = _task_payload(first)
    assert first_body["awaiting_dispatch"] is False
    second = client.post(
        f"/v1/tasks/{task['id']}/dispatch", json=payload, headers=auth_header("test-dispatch")
    )
    assert second.status_code == 200, second.text
    assert _task_payload(second)["version"] == first_body["version"]


def _events(client, auth_header, task_id: str) -> list[dict]:
    resp = client.get(f"/v1/tasks/{task_id}/events", headers=auth_header("test-read"))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _payloads_have_token(events: list[dict], token_id: str) -> bool:
    return any(
        isinstance(item.get("payload"), dict) and item["payload"].get("token_id") == token_id
        for item in events
    )


def _payloads_leak_secrets(events: list[dict]) -> bool:
    forbidden = {"secret", "secret_sha256", "authorization", "bearer"}
    for item in events:
        payload = item.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        keys = {str(key).lower() for key in payload}
        if keys & forbidden:
            return True
        for value in payload.values():
            if isinstance(value, str) and "secret" in value.lower() and "sha" not in value.lower():
                # only flag obvious plaintext secret-like values, not token_id
                if value.endswith("-secret") or "test-approve-secret" in value or "test-dispatch-secret" in value:
                    return True
    return False


def test_approve_subset_scope_is_idempotent_and_does_not_shrink_grant(
    client, auth_header, make_task_body
):
    """Design: 审批 tag 范围已覆盖 → 请求范围已被现有 grant 覆盖时幂等，不得缩小范围。"""
    task = _waiting_approval(client, auth_header, make_task_body, tags=["payment"])
    first = client.post(
        f"/v1/tasks/{task['id']}/approve",
        json=_write_body(task, scope=["payment", "publish"]),
        headers=auth_header("test-approve"),
    )
    assert first.status_code == 200, first.text
    granted = _task_payload(first)
    assert set(granted["approval_scope"]) >= {"payment", "publish"}
    # Re-send a narrower request that still covers required tags, with the stale first body version.
    second = client.post(
        f"/v1/tasks/{task['id']}/approve",
        json=_write_body(task, scope=["payment"]),
        headers=auth_header("test-approve"),
    )
    assert second.status_code == 200, second.text
    body = _task_payload(second)
    assert body["version"] == granted["version"]
    assert set(body["approval_scope"]) >= {"payment", "publish"}


def test_approve_superset_scope_with_stale_version_is_not_idempotent(
    client, auth_header, make_task_body
):
    """Wider scope is a new grant, not a replay; stale expected_version must 409 and not expand."""
    task = _waiting_approval(client, auth_header, make_task_body, tags=["payment"])
    first = client.post(
        f"/v1/tasks/{task['id']}/approve",
        json=_write_body(task, scope=["payment"]),
        headers=auth_header("test-approve"),
    )
    assert first.status_code == 200, first.text
    granted = _task_payload(first)
    assert granted["approval_scope"] == ["payment"] or set(granted["approval_scope"]) == {"payment"}
    stale = client.post(
        f"/v1/tasks/{task['id']}/approve",
        json=_write_body(task, scope=["payment", "publish"]),
        headers=auth_header("test-approve"),
    )
    assert stale.status_code == 409, stale.text
    again = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read")).json()
    assert again["version"] == granted["version"]
    assert set(again["approval_scope"]) == {"payment"}


def test_approve_superset_scope_with_current_version_applies_new_grant(
    client, auth_header, make_task_body
):
    task = _waiting_approval(client, auth_header, make_task_body, tags=["payment"])
    first = client.post(
        f"/v1/tasks/{task['id']}/approve",
        json=_write_body(task, scope=["payment"]),
        headers=auth_header("test-approve"),
    )
    assert first.status_code == 200, first.text
    granted = _task_payload(first)
    wider = client.post(
        f"/v1/tasks/{task['id']}/approve",
        json=_write_body(granted, scope=["payment", "publish"]),
        headers=auth_header("test-approve"),
    )
    assert wider.status_code == 200, wider.text
    body = _task_payload(wider)
    assert body["version"] == granted["version"] + 1
    assert set(body["approval_scope"]) >= {"payment", "publish"}


def test_approve_replay_with_different_token_is_not_idempotent(
    client, auth_header, make_task_body
):
    task = _waiting_approval(client, auth_header, make_task_body, tags=["payment"])
    payload = _write_body(task, scope=["payment"])
    first = client.post(
        f"/v1/tasks/{task['id']}/approve", json=payload, headers=auth_header("test-approve")
    )
    assert first.status_code == 200, first.text
    granted = _task_payload(first)
    other = client.post(
        f"/v1/tasks/{task['id']}/approve", json=payload, headers=auth_header("test-approve-alt")
    )
    assert other.status_code == 409, other.text
    again = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read")).json()
    assert again["version"] == granted["version"]


def test_dispatch_success_journals_token_id_without_secrets(
    client, auth_header, make_task_body
):
    task = _create(client, auth_header, make_task_body, require_dispatch=True)
    resp = client.post(
        f"/v1/tasks/{task['id']}/dispatch",
        json=_write_body(task),
        headers=auth_header("test-dispatch"),
    )
    assert resp.status_code == 200, resp.text
    events = _events(client, auth_header, task["id"])
    assert _payloads_have_token(events, "test-dispatch")
    assert not _payloads_leak_secrets(events)


def test_dispatch_cleared_gate_wrong_hash_returns_409(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body, require_dispatch=True)
    first = client.post(
        f"/v1/tasks/{task['id']}/dispatch",
        json=_write_body(task),
        headers=auth_header("test-dispatch"),
    )
    assert first.status_code == 200, first.text
    opened = _task_payload(first)
    wrong = client.post(
        f"/v1/tasks/{task['id']}/dispatch",
        json=_write_body(opened, content_hash="0" * 64),
        headers=auth_header("test-dispatch"),
    )
    assert wrong.status_code == 409, wrong.text
    again = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read")).json()
    assert again["version"] == opened["version"]
    assert again["awaiting_dispatch"] is False


def test_dispatch_after_cancel_returns_409(client, auth_header, make_task_body):
    """Gate already cleared, but cancelled is not an allowed dispatch idempotent state."""
    task = _create(client, auth_header, make_task_body, require_dispatch=True)
    dispatched = client.post(
        f"/v1/tasks/{task['id']}/dispatch",
        json=_write_body(task),
        headers=auth_header("test-dispatch"),
    )
    assert dispatched.status_code == 200, dispatched.text
    opened = _task_payload(dispatched)
    cancelled = client.post(
        f"/v1/tasks/{task['id']}/cancel",
        json=_write_body(opened),
        headers=auth_header("test-cancel"),
    )
    # Until cancel accepts expected_version/content_hash, this may 4xx; after it must cancel.
    if cancelled.status_code == 200:
        done = _task_payload(cancelled)
    else:
        # Fall back is not allowed for the assertion of this test once cancel CAS lands;
        # require 200 so this test tracks the cancel contract as well.
        assert cancelled.status_code == 200, cancelled.text
        done = _task_payload(cancelled)
    assert done["status"] == "cancelled"
    replay = client.post(
        f"/v1/tasks/{task['id']}/dispatch",
        json=_write_body(done),
        headers=auth_header("test-dispatch"),
    )
    assert replay.status_code == 409, replay.text
    again = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read")).json()
    assert again["status"] == "cancelled"
    assert again["version"] == done["version"]


def test_cancel_requires_version_and_hash(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body)
    missing = client.post(
        f"/v1/tasks/{task['id']}/cancel",
        json={},
        headers=auth_header("test-cancel"),
    )
    assert missing.status_code in (400, 422)
    again = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read")).json()
    assert again["status"] != "cancelled"
    assert again["version"] == task["version"]


def test_cancel_stale_version_or_wrong_hash_returns_409(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body)
    stale = client.post(
        f"/v1/tasks/{task['id']}/cancel",
        json=_write_body(task, expected_version=task["version"] + 99),
        headers=auth_header("test-cancel"),
    )
    assert stale.status_code == 409, stale.text
    wrong = client.post(
        f"/v1/tasks/{task['id']}/cancel",
        json=_write_body(task, content_hash="0" * 64),
        headers=auth_header("test-cancel"),
    )
    assert wrong.status_code == 409, wrong.text
    again = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read")).json()
    assert again["status"] != "cancelled"
    assert again["version"] == task["version"]


def test_cancel_matching_request_succeeds_and_replay_stays_409(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body)
    first = client.post(
        f"/v1/tasks/{task['id']}/cancel",
        json=_write_body(task),
        headers=auth_header("test-cancel"),
    )
    assert first.status_code == 200, first.text
    body = _task_payload(first)
    assert body["status"] == "cancelled"
    events = _events(client, auth_header, task["id"])
    assert _payloads_have_token(events, "test-cancel")
    assert not _payloads_leak_secrets(events)
    replay = client.post(
        f"/v1/tasks/{task['id']}/cancel",
        json=_write_body(body),
        headers=auth_header("test-cancel"),
    )
    assert replay.status_code == 409, replay.text
    again = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read")).json()
    assert again["status"] == "cancelled"
    assert again["version"] == body["version"]


def test_finalize_after_cancel_does_not_resurrect(db_url):
    from max_agent.db.session import get_session_factory, session_scope
    from max_agent.kernel.errors import StaleFenceError
    from max_agent.kernel.scheduler import Scheduler
    from max_agent.kernel.service import TaskService
    from max_agent.models.enums import TaskStatus, WorkerKind
    from max_agent.models.schemas import TaskCreate, WorkerResult

    with session_scope(db_url) as session:
        created = TaskService(session).create(
            TaskCreate(
                goal="cancel then finalize",
                acceptance_criteria=["worker_ok"],
                worker_kind=WorkerKind.MOCK,
                worker_payload={"mode": "success", "answer": "ok"},
            )
        )
        task_id = created.id

    factory = get_session_factory(db_url)
    sched = Scheduler(session_factory=factory, worker_id="owner")
    claim = sched.acquire_lease(task_id)

    with session_scope(db_url) as session:
        cancelled = TaskService(session).cancel(task_id)
        assert cancelled.status == TaskStatus.CANCELLED

    with pytest.raises(StaleFenceError):
        sched.finalize_lease(
            claim,
            WorkerResult(ok=True, summary="late finalize", outputs={"answer": "ok"}),
        )

    with session_scope(db_url) as session:
        again = TaskService(session).get(task_id)
        assert again.status == TaskStatus.CANCELLED
        assert again.result_summary != "late finalize"
