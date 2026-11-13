"""In-process staging smoke. Talks to the app, not to a production host."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path

from max_agent.db.session import init_db, reset_engine

_DATABASE_ENV_VAR = "MAX_AGENT_DATABASE_URL"
_REGISTRY_ENV_VAR = "MAX_AGENT_API_TOKEN_REGISTRY"
_TOKEN_ENV_VAR = "MAX_AGENT_API_TOKEN"

_SMOKE_TOKEN_ID = "smoke-create"
_SMOKE_SCOPES = ["create"]


def _write_temp_registry(directory: str) -> tuple[Path, str]:
    """Write a process-local registry holding a single create-scoped token.

    Returns the registry path and the plaintext secret.
    """
    secret = secrets.token_urlsafe(32)
    payload = {
        _SMOKE_TOKEN_ID: {
            "secret_sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
            "scopes": list(_SMOKE_SCOPES),
            "enabled": True,
        }
    }
    path = Path(directory) / "smoke-token-registry.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, secret


def run_smoke(*, database_url: str) -> dict[str, object]:
    previous_database = os.environ.get(_DATABASE_ENV_VAR)
    previous_registry = os.environ.get(_REGISTRY_ENV_VAR)
    previous_token = os.environ.get(_TOKEN_ENV_VAR)

    os.environ[_DATABASE_ENV_VAR] = database_url
    # The legacy single all-powerful token must not be usable any more.
    os.environ.pop(_TOKEN_ENV_VAR, None)

    reset_engine()
    with tempfile.TemporaryDirectory(prefix="max-agent-smoke-") as tmp_dir:
        registry_path, secret = _write_temp_registry(tmp_dir)
        os.environ[_REGISTRY_ENV_VAR] = str(registry_path)
        try:
            init_db(database_url)
            from fastapi.testclient import TestClient

            from max_agent.api.app import create_app

            with TestClient(create_app()) as client:
                health = client.get("/v1/health")
                ready = client.get("/v1/ready")
                created = client.post(
                    "/v1/tasks",
                    headers={"Authorization": f"Bearer {secret}"},
                    json={
                        "goal": "staging smoke",
                        "acceptance_criteria": ["worker_ok"],
                        "worker_kind": "mock",
                        "worker_payload": {"mode": "success", "answer": "ok"},
                        "idempotency_key": "staging-smoke",
                    },
                )
            if (
                health.status_code != 200
                or ready.status_code != 200
                or created.status_code != 200
            ):
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
            if previous_database is None:
                os.environ.pop(_DATABASE_ENV_VAR, None)
            else:
                os.environ[_DATABASE_ENV_VAR] = previous_database
            if previous_registry is None:
                os.environ.pop(_REGISTRY_ENV_VAR, None)
            else:
                os.environ[_REGISTRY_ENV_VAR] = previous_registry
            if previous_token is None:
                os.environ.pop(_TOKEN_ENV_VAR, None)
            else:
                os.environ[_TOKEN_ENV_VAR] = previous_token
