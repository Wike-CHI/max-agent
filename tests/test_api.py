from __future__ import annotations

import os

from fastapi.testclient import TestClient

from max_agent.api.app import create_app
from max_agent.db.session import init_db, reset_engine


def test_api_create_run_events(tmp_path):
    url = f"sqlite:///{tmp_path / 'api.db'}"
    os.environ["MAX_AGENT_DATABASE_URL"] = url
    reset_engine()
    init_db(url)
    client = TestClient(create_app(), headers={"Authorization": "Bearer test-api-token"})

    r = client.get("/v1/health")
    assert r.status_code == 200

    r = client.post(
        "/v1/tasks",
        json={
            "goal": "api demo",
            "acceptance_criteria": ["worker_ok"],
            "worker_kind": "mock",
            "worker_payload": {"mode": "success", "answer": "x"},
            "idempotency_key": "api-1",
        },
    )
    assert r.status_code == 200
    task = r.json()
    tid = task["id"]

    r = client.post(f"/v1/tasks/{tid}/schedule", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "succeeded"

    r = client.get(f"/v1/tasks/{tid}/events")
    assert r.status_code == 200
    assert len(r.json()) >= 3

    reset_engine()
    os.environ.pop("MAX_AGENT_DATABASE_URL", None)
