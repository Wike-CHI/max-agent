#!/usr/bin/env python3
"""Simulate a Grok Bot calling MAX. One create, one worker run, one status read.

The bot does not loop on status. `run` is the local worker step, not the caller.
Safe mock worker only: no production credentials, no remote write.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _call(args: list[str]) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "max_agent.cli.main", "--json", *args],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout)
        return {"ok": False, "error": (proc.stdout or proc.stderr).strip(), "args": args}
    return json.loads(proc.stdout)


def _prepare_db() -> None:
    if os.environ.get("MAX_AGENT_DATABASE_URL"):
        return
    path = Path(tempfile.mkdtemp(prefix="max-grok-")) / "demo.db"
    os.environ["MAX_AGENT_DATABASE_URL"] = f"sqlite:///{path}"
    os.environ.setdefault("MAX_AGENT_WEBHOOK_SECRET", "demo-not-production")


def _webhook(task_goal: str) -> dict:
    from fastapi.testclient import TestClient

    from max_agent.api.app import create_app
    from max_agent.db.session import reset_engine
    from max_agent.entry.webhook import sign_body

    reset_engine()
    client = TestClient(create_app())
    body = json.dumps(
        {
            "idempotency_key": "demo-routine-1",
            "goal": task_goal,
            "acceptance_criteria": ["worker_ok"],
            "worker_kind": "mock",
            "worker_payload": {"mode": "success", "answer": "routine"},
        }
    ).encode()
    stamp = str(int(time.time()))
    secret = os.environ["MAX_AGENT_WEBHOOK_SECRET"]
    signature = sign_body(secret, stamp, body)
    headers = {
        "content-type": "application/json",
        "X-Max-Timestamp": stamp,
        "X-Max-Signature": signature,
        "X-Max-Delivery-Id": "demo-delivery-1",
    }
    first = client.post("/v1/inbound/webhook", content=body, headers=headers)
    replay_headers = dict(headers)
    replay = client.post("/v1/inbound/webhook", content=body, headers=replay_headers)
    other = dict(headers)
    other["X-Max-Delivery-Id"] = "demo-delivery-2"
    associated = client.post("/v1/inbound/webhook", content=body, headers=other)
    return {
        "first": first.json(),
        "replay_deduplicated": replay.json().get("deduplicated"),
        "same_task": replay.json().get("task_id") == first.json().get("task_id"),
        "associated_task_id": associated.json().get("task_id"),
        "associated_idempotent_hit": associated.json().get("idempotent_hit"),
    }


def main() -> int:
    _prepare_db()
    created = _call(
        [
            "create",
            "grok demo hello",
            "-c",
            "worker_ok",
            "-c",
            "output_equals:answer=hello",
            "-w",
            "mock",
            "--payload",
            json.dumps({"mode": "success", "answer": "hello"}),
            "-k",
            "grok-demo-hello",
        ]
    )
    if not created.get("ok"):
        print(json.dumps(created, indent=2))
        return 1
    task_id = created["task_id"]
    # Single worker invocation. The bot is not asked to poll.
    ran = _call(["run", task_id])
    status = _call(["status", task_id])
    events = _call(["events", task_id])

    blocked = _call(
        [
            "create",
            "publish demo",
            "-c",
            "worker_ok",
            "-w",
            "mock",
            "--payload",
            json.dumps({"mode": "success", "answer": "ok"}),
            "--tag",
            "publish",
            "-k",
            "grok-demo-publish",
        ]
    )
    still_blocked = _call(["run", blocked["task_id"], "--confirm-side-effects"])
    approved = _call(["approve", blocked["task_id"], "--by", "demo-user", "--note", "local mock only"])
    published = _call(["run", blocked["task_id"]])
    inbound = _webhook("routine demo")

    report = {
        "caller_poll_loops": 0,
        "create": {
            "task_id": created.get("task_id"),
            "status": created.get("status"),
            "attempts": (created.get("task") or {}).get("attempts"),
        },
        "worker_run_status": ran.get("status"),
        "status_once": {
            "task_id": status.get("task_id"),
            "status": status.get("status"),
            "verification": status.get("verification"),
        },
        "event_types": [item.get("event_type") for item in (events.get("events") or [])],
        "approval": {
            "before": blocked.get("status"),
            "run_without_approve": still_blocked.get("status"),
            "after_approve": approved.get("status"),
            "after_approved_run": published.get("status"),
        },
        "webhook": {
            "task_id": (inbound.get("first") or {}).get("task_id"),
            "replay_deduplicated": inbound.get("replay_deduplicated"),
            "same_task": inbound.get("same_task"),
            "associated_idempotent_hit": inbound.get("associated_idempotent_hit"),
            "associated_same_task": inbound.get("associated_task_id") == (inbound.get("first") or {}).get("task_id"),
        },
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    ok = (
        created.get("status") == "ready"
        and (created.get("task") or {}).get("attempts") == 0
        and status.get("status") == "succeeded"
        and (status.get("verification") or {}).get("passed") is True
        and status.get("task_id") == task_id
        and blocked.get("approval_required") is True
        and still_blocked.get("status") == "blocked"
        and published.get("status") == "succeeded"
        and inbound.get("replay_deduplicated") is True
        and inbound.get("same_task") is True
        and inbound.get("associated_idempotent_hit") is True
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
