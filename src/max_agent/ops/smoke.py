"""In-process staging smoke. Talks to the app, not to a production host."""

from __future__ import annotations

import os

from max_agent.db.session import init_db, reset_engine


def run_smoke(*, database_url: str) -> dict[str, object]:
    previous = os.environ.get("MAX_AGENT_DATABASE_URL")
    os.environ["MAX_AGENT_DATABASE_URL"] = database_url
    reset_engine()
    try:
        init_db(database_url)
        from fastapi.testclient import TestClient

        from max_agent.api.app import create_app

        with TestClient(create_app()) as client:
            health = client.get("/v1/health")
            ready = client.get("/v1/ready")
            created = client.post(
                "/v1/tasks",
                json={
                    "goal": "staging smoke",
                    "acceptance_criteria": ["worker_ok"],
                    "worker_kind": "mock",
                    "worker_payload": {"mode": "success", "answer": "ok"},
                    "idempotency_key": "staging-smoke",
                },
            )
        if health.status_code != 200 or ready.status_code != 200 or created.status_code != 200:
            raise RuntimeError(
                f"smoke failed: health={health.status_code} ready={ready.status_code} create={created.status_code}"
            )
        task_id = created.json().get("id")
        if not task_id:
            raise RuntimeError("smoke create returned no task id")
        return {
            "health": health.status_code,
            "ready": ready.status_code,
            "task_id": task_id,
            "deploy_executed": False,
        }
    finally:
        reset_engine()
        if previous is None:
            os.environ.pop("MAX_AGENT_DATABASE_URL", None)
        else:
            os.environ["MAX_AGENT_DATABASE_URL"] = previous
