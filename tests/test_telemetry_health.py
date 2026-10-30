"""Liveness, readiness, in-process telemetry, and SLO targets."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from max_agent import __version__
from max_agent.api.app import create_app
from max_agent.db.session import init_db, reset_engine
from max_agent.ops.health import DatabaseUnavailable
from max_agent.ops.slo import UNAUTHORIZED_DEPLOYS, JOURNAL_RESTORE_COMPLETENESS, READINESS_SUCCESS_RATIO
from max_agent.ops.telemetry import get_telemetry, reset_telemetry


@pytest.fixture()
def client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    url = f"sqlite:///{tmp_path / 'health.db'}"
    monkeypatch.setenv("MAX_AGENT_DATABASE_URL", url)
    monkeypatch.setenv("MAX_AGENT_WEBHOOK_SECRET", "webhook-secret-value-should-not-leak")
    reset_engine()
    reset_telemetry()
    init_db(url)
    with TestClient(create_app(), headers={"Authorization": "Bearer test-api-token"}) as test_client:
        yield test_client
    reset_engine()
    reset_telemetry()


def test_health_is_liveness_and_ready_checks_database(client: TestClient) -> None:
    health = client.get("/v1/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert "webhook-secret-value-should-not-leak" not in health.text

    ready = client.get("/v1/ready")
    assert ready.status_code == 200
    assert ready.json()["database"] == "ok"
    assert "webhook-secret-value-should-not-leak" not in ready.text


def test_ready_failure_hides_secret_text(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode() -> None:
        raise DatabaseUnavailable("database unavailable: password=hunter2secret")

    monkeypatch.setattr("max_agent.api.routes.database_ready", explode)
    response = client.get("/v1/ready")
    assert response.status_code == 503
    assert "hunter2secret" not in response.text
    assert response.json()["detail"] == "database unavailable"


def test_requests_record_spans_without_otel_sdk(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_AGENT_OTEL", "1")
    reset_telemetry()
    response = client.get("/v1/health")
    assert response.status_code == 200
    snapshot = get_telemetry().snapshot()
    assert snapshot["counters"]["http.server.requests"] >= 1
    assert any(span["name"] == "http.request" and span["status"] == "ok" for span in snapshot["spans"])
    listed = client.get("/v1/telemetry")
    assert listed.status_code == 200
    assert "http.server.requests" in listed.json()["counters"]


def test_slo_targets_match_the_release_bar() -> None:
    assert READINESS_SUCCESS_RATIO == 0.99
    assert JOURNAL_RESTORE_COMPLETENESS == 1.0
    assert UNAUTHORIZED_DEPLOYS == 0
    text = (os.path.join(os.path.dirname(__file__), "..", "docs", "operations", "slo.md"))
    body = open(text, encoding="utf-8").read()
    assert "0.99" in body
    assert "restore" in body.lower()
