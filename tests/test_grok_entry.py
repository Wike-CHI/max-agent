"""Grok Bot entry: one-shot JSON contract, approval gate, signed inbound events."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from max_agent.cli.main import app
from max_agent.db.session import reset_engine
from max_agent.policy.approval import requires_human_approval


runner = CliRunner()


def test_sensitive_tags_need_persisted_approval_not_a_run_flag():
    need, reason = requires_human_approval(
        tags=["Publish"],
        has_side_effects=False,
        is_retry=False,
        confirm_side_effects=True,
        allow_auto_retry_side_effects=False,
        approval_granted=False,
    )
    assert need is True
    assert "publish" in reason

    need, _ = requires_human_approval(
        tags=["payment", "external_write"],
        has_side_effects=True,
        is_retry=True,
        confirm_side_effects=False,
        allow_auto_retry_side_effects=False,
        approval_granted=True,
    )
    assert need is False


def _invoke(db_url: str, args: list[str]) -> dict:
    env = os.environ.copy()
    env["MAX_AGENT_DATABASE_URL"] = db_url
    result = runner.invoke(app, args, env=env)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_create_returns_task_id_without_running(db_url: str):
    created = _invoke(
        db_url,
        [
            "--json",
            "create",
            "grok says hello",
            "-c",
            "worker_ok",
            "-c",
            "output_equals:answer=hello",
            "-w",
            "mock",
            "--payload",
            json.dumps({"mode": "success", "answer": "hello"}),
            "-k",
            "grok-demo-1",
        ],
    )
    assert created["ok"] is True
    assert created["command"] == "create"
    assert created["task_id"]
    assert created["status"] == "ready"
    assert created["approval_required"] is False
    assert created["verification"] is None
    assert created["task"]["attempts"] == 0

    again = _invoke(
        db_url,
        [
            "--json",
            "create",
            "grok says hello",
            "-c",
            "worker_ok",
            "-k",
            "grok-demo-1",
        ],
    )
    assert again["task_id"] == created["task_id"]
    assert again["idempotent_hit"] is True

    legacy = _invoke(
        db_url,
        ["create", "legacy shape", "-c", "worker_ok", "-k", "grok-legacy"],
    )
    assert legacy["id"]
    assert "task_id" not in legacy


def test_one_status_read_returns_verification(db_url: str):
    created = _invoke(
        db_url,
        [
            "--json",
            "create",
            "verify once",
            "-c",
            "worker_ok",
            "-w",
            "mock",
            "--payload",
            json.dumps({"mode": "success", "answer": "ok"}),
            "-k",
            "grok-once",
        ],
    )
    task_id = created["task_id"]
    ran = _invoke(db_url, ["--json", "run", task_id])
    assert ran["status"] == "succeeded"

    status = _invoke(db_url, ["--json", "status", task_id])
    events = _invoke(db_url, ["--json", "events", task_id])
    assert status["task_id"] == task_id
    assert status["status"] == "succeeded"
    assert status["verification"]["passed"] is True
    assert any(event["event_type"] == "verification" for event in events["events"])

    cancelled = _invoke(
        db_url,
        [
            "--json",
            "create",
            "cancel me",
            "-c",
            "worker_ok",
            "-k",
            "grok-cancel",
        ],
    )
    stopped = _invoke(db_url, ["--json", "cancel", cancelled["task_id"]])
    assert stopped["status"] == "cancelled"


def test_publish_stays_blocked_until_approve(db_url: str):
    created = _invoke(
        db_url,
        [
            "--json",
            "create",
            "publish notes",
            "-c",
            "worker_ok",
            "-w",
            "mock",
            "--payload",
            json.dumps({"mode": "success", "answer": "ok"}),
            "--tag",
            "publish",
            "--side-effects",
            "-k",
            "grok-publish",
        ],
    )
    assert created["status"] == "waiting_approval"
    assert created["approval_required"] is True

    blocked = _invoke(db_url, ["--json", "run", created["task_id"], "--confirm-side-effects"])
    assert blocked["status"] == "waiting_approval"
    assert blocked["approval_required"] is True

    approved = _invoke(
        db_url,
        ["--json", "approve", created["task_id"], "--by", "grok-user", "--note", "publish ok"],
    )
    assert approved["approved"] is True
    assert approved["approval_required"] is False
    assert approved["status"] == "ready"

    ran = _invoke(db_url, ["--json", "run", created["task_id"]])
    assert ran["status"] == "succeeded"
    assert ran["verification"]["passed"] is True


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return digest


def test_webhook_disabled_without_secret(db_url: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAX_AGENT_DATABASE_URL", db_url)
    monkeypatch.delenv("MAX_AGENT_WEBHOOK_SECRET", raising=False)
    reset_engine()
    from max_agent.api.app import create_app

    client = TestClient(create_app())
    response = client.post(
        "/v1/inbound/webhook",
        content=b"{}",
        headers={"X-Max-Delivery-Id": "no-secret", "X-Max-Timestamp": str(int(time.time())), "X-Max-Signature": "ab"},
    )
    assert response.status_code == 503


def test_webhook_verifies_dedups_and_associates_idempotency_key(db_url: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAX_AGENT_DATABASE_URL", db_url)
    monkeypatch.setenv("MAX_AGENT_WEBHOOK_SECRET", "unit-test-secret")
    reset_engine()
    from max_agent.api.app import create_app

    client = TestClient(create_app())
    body = json.dumps(
        {
            "idempotency_key": "routine-issue-5",
            "goal": "routine mock",
            "acceptance_criteria": ["worker_ok"],
            "worker_kind": "mock",
            "worker_payload": {"mode": "success", "answer": "routine"},
        }
    ).encode()
    stamp = str(int(time.time()))

    def post(delivery_id: str, payload: bytes = body, signature: str | None = None, timestamp: str = stamp):
        sig = signature if signature is not None else _sign("unit-test-secret", timestamp, payload)
        return client.post(
            "/v1/inbound/webhook",
            content=payload,
            headers={
                "content-type": "application/json",
                "X-Max-Timestamp": timestamp,
                "X-Max-Signature": sig,
                "X-Max-Delivery-Id": delivery_id,
            },
        )

    bad = post("delivery-1", signature="0" * 64)
    assert bad.status_code == 401

    first = post("delivery-1")
    assert first.status_code == 200
    payload = first.json()
    assert payload["ok"] is True
    assert payload["deduplicated"] is False
    assert payload["idempotency_key"] == "routine-issue-5"
    assert payload["status"] in {"ready", "blocked"}
    assert payload["task"]["attempts"] == 0

    replay = post("delivery-1")
    assert replay.status_code == 200
    assert replay.json()["deduplicated"] is True
    assert replay.json()["task_id"] == payload["task_id"]

    associated = post("delivery-2")
    assert associated.status_code == 200
    assert associated.json()["deduplicated"] is False
    assert associated.json()["task_id"] == payload["task_id"]
    assert associated.json()["idempotent_hit"] is True

    stale = post("delivery-3", timestamp="100")
    assert stale.status_code == 401


def test_mcp_tool_call_returns_task_id(db_url: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAX_AGENT_DATABASE_URL", db_url)
    reset_engine()
    from max_agent.mcp.server import handle_message

    listed = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert {"max_create", "max_status", "max_events", "max_cancel", "max_approve"} <= names

    created = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "max_create",
                "arguments": {
                    "goal": "mcp mock",
                    "acceptance_criteria": ["worker_ok"],
                    "worker_kind": "mock",
                    "worker_payload": {"mode": "success", "answer": "mcp"},
                    "idempotency_key": "mcp-1",
                },
            },
        }
    )
    text = created["result"]["content"][0]["text"]
    envelope = json.loads(text)
    assert envelope["task_id"]
    assert envelope["status"] == "ready"
    assert created["result"]["isError"] is False

    ignored = handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert ignored is None


def test_demo_script_prints_contract(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "grok_entry_demo.py"
    monkeypatch.setenv("MAX_AGENT_DATABASE_URL", db_url)
    monkeypatch.setenv("MAX_AGENT_WEBHOOK_SECRET", "demo-not-production")
    reset_engine()
    import runpy

    ns = runpy.run_path(str(script))
    code = ns["main"]()
    assert code == 0
