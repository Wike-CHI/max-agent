from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from max_agent.api.app import create_app


@pytest.fixture()
def client(db_url: str):
    with TestClient(create_app()) as test_client:
        yield test_client


def test_task_read_exposes_awaiting_dispatch_and_version(client, auth_header, make_task_body):
    resp = client.post("/v1/tasks", json=make_task_body(worker_kind="mock", worker_payload={"mode": "success"}), headers=auth_header("test-create"))
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["version"], int)
    assert body["awaiting_dispatch"] is False
