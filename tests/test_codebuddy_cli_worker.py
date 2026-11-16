"""CodeBuddy is an explicit, fail-closed MAX CliWorker vendor (Issue #69).

The fake binary exercises the real MAX Task -> lease -> CliWorker ->
independent pytest -> Journal path. No paid CLI or network is used.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.models.enums import EventType, TaskStatus, WorkerKind
from max_agent.models.schemas import TaskCreate
from max_agent.workers.cli_adapter import (
    Probe,
    _PROBE_ORDER,
    build_exec_argv,
    choose_vendor,
    probe_vendors,
)
from max_agent.workers.sandbox import ALLOWED_PASSTHROUGH, child_env


@pytest.fixture(autouse=True)
def _deny_cli_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAX_AGENT_ALLOW_EXTERNAL_CLI", raising=False)
    monkeypatch.delenv("CODEBUDDY_API_KEY", raising=False)
    monkeypatch.delenv("MAX_AGENT_REQUIRE_REAL_WORKER", raising=False)


def _run(session, *, workspace_root: Path):
    task = TaskService(session).create(
        TaskCreate(
            goal="Implement add(a,b) with a CodeBuddy worker",
            acceptance_criteria=["worker_ok", "output_equals:pytest_exit_code=0"],
            worker_kind=WorkerKind.CLI,
            worker_payload={
                "vendor": "codebuddy",
                "workspace_root": str(workspace_root),
                "require_real_worker": True,
            },
            timeout_seconds=60,
        )
    )
    return task, Scheduler(session).run_one(task.id)


def _worker_outputs(session, task_id: str) -> dict:
    events = TaskService(session).list_events(task_id, limit=500)
    for event in reversed(events):
        if event.event_type == EventType.WORKER_OUTPUT:
            return dict(event.payload.get("outputs") or {})
    raise AssertionError("missing WORKER_OUTPUT journal event")


def _fake_codebuddy(binary: Path, audit: Path) -> None:
    binary.write_text(
        f"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("codebuddy-fixture 1.0")
    raise SystemExit(0)

Path({str(audit)!r}).write_text(json.dumps({{
    "args": ["-p" if value == "-p" else "--output-format" if value == "--output-format" else "[prompt]" for value in args],
    "has_codebuddy_key": bool(os.environ.get("CODEBUDDY_API_KEY")),
    "has_github_token": "GITHUB_TOKEN" in os.environ,
    "has_max_db": "MAX_AGENT_DATABASE_URL" in os.environ,
    "autoupdate_disabled": os.environ.get("CODEBUDDY_DISABLE_AUTOUPDATE") == "1",
}}))
Path.cwd().joinpath("calc.py").write_text("def add(a: int, b: int) -> int:\\n    return a + b\\n")
print(json.dumps({{
    "type": "result", "model": "fixture-codebuddy",
    "usage": {{
        "source": "cli_quota", "marker": "measured", "amount": 0.25,
        "input_tokens": 40, "output_tokens": 30
    }}
}}))
""",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)


def test_codebuddy_available_only_as_explicit_vendor() -> None:
    enabled = [Probe(vendor="codebuddy", available=True, enabled=True, reason="ok")]
    assert "codebuddy" in _PROBE_ORDER
    assert choose_vendor("codebuddy", enabled, authorized=True) == ("codebuddy", None)
    assert choose_vendor("auto", enabled, authorized=True) == ("stub", None)
    assert choose_vendor("codebuddy", enabled, authorized=False) == ("codebuddy", "cli_not_authorized")


def test_codebuddy_headless_argv_does_not_use_bypass_flags() -> None:
    prompt = "Fix a test; $(touch /tmp/untrusted-command)"
    argv = build_exec_argv(
        "codebuddy",
        binary="/usr/local/bin/codebuddy",
        workspace=Path("/tmp/max-agent-workspaces/work"),
        prompt=prompt,
        prompt_file=Path("/tmp/max-agent-workspaces/work/.max-agent/prompt.txt"),
    )
    assert argv == ["/usr/local/bin/codebuddy", "-p", prompt, "--output-format", "stream-json"]
    assert "--yolo" not in argv
    assert "--dangerously-skip-permissions" not in argv


def test_codebuddy_environment_is_explicitly_allowlisted(tmp_path: Path) -> None:
    assert "CODEBUDDY_API_KEY" in ALLOWED_PASSTHROUGH
    env = child_env(
        home=tmp_path / "home",
        tmp=tmp_path / "tmp",
        passthrough={"CODEBUDDY_API_KEY": "fixture-codebuddy-secret", "GITHUB_TOKEN": "forbidden"},
    )
    assert env.get("CODEBUDDY_API_KEY") == "fixture-codebuddy-secret"
    assert "GITHUB_TOKEN" not in env


def test_unauthorized_codebuddy_never_runs(session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    marker = tmp_path / "invoked.json"
    _fake_codebuddy(binary_dir / "codebuddy", marker)
    monkeypatch.setenv("PATH", f"{binary_dir}{os.pathsep}/usr/bin:/bin")
    workspace_root = tmp_path / "workspace"
    task, result = _run(session, workspace_root=workspace_root)
    assert result.status == TaskStatus.FAILED
    outputs = _worker_outputs(session, task.id)
    assert outputs["adapter_failure"] == "cli_not_authorized"
    assert outputs["real_cli_invoked"] is False
    assert not marker.exists()
    assert not workspace_root.exists()


def test_missing_codebuddy_key_fails_before_workspace(
    session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    marker = tmp_path / "invoked.json"
    _fake_codebuddy(binary_dir / "codebuddy", marker)
    monkeypatch.setenv("PATH", f"{binary_dir}{os.pathsep}/usr/bin:/bin")
    monkeypatch.setenv("MAX_AGENT_ALLOW_EXTERNAL_CLI", "1")
    workspace_root = tmp_path / "workspace"
    task, result = _run(session, workspace_root=workspace_root)
    assert result.status == TaskStatus.FAILED
    outputs = _worker_outputs(session, task.id)
    assert outputs["adapter_failure"] == "cli_auth_missing"
    assert outputs["real_cli_invoked"] is False
    assert not marker.exists()
    assert not workspace_root.exists()


def test_codebuddy_fake_binary_finishes_real_worker_path_with_independent_pytest(
    session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    audit = tmp_path / "invoked.json"
    _fake_codebuddy(binary_dir / "codebuddy", audit)
    monkeypatch.setenv("PATH", f"{binary_dir}{os.pathsep}/usr/bin:/bin")
    monkeypatch.setenv("MAX_AGENT_ALLOW_EXTERNAL_CLI", "1")
    monkeypatch.setenv("CODEBUDDY_API_KEY", "codebuddy-fixture-secret-123")
    monkeypatch.setenv("GITHUB_TOKEN", "github-fixture-secret-123")
    task, result = _run(session, workspace_root=tmp_path / "workspace")
    assert result.status == TaskStatus.SUCCEEDED, result.last_error
    outputs = _worker_outputs(session, task.id)
    assert outputs["vendor"] == "codebuddy"
    assert outputs["model"] == "fixture-codebuddy"
    assert outputs["provider"] == "codebuddy"
    assert outputs["real_cli_invoked"] is True
    assert outputs["require_real_worker"] is True
    assert outputs["pytest_exit_code"] == 0
    assert outputs["usage"]["amount"] == 0.25
    assert outputs["usage"]["source"] == "cli_quota"
    assert outputs["usage"]["input_tokens"] == 40
    assert outputs["usage"]["output_tokens"] == 30
    assert "codebuddy-fixture-secret-123" not in json.dumps(outputs)
    assert "github-fixture-secret-123" not in json.dumps(outputs)
    record = json.loads(audit.read_text(encoding="utf-8"))
    assert record["has_codebuddy_key"] is True
    assert record["has_github_token"] is False
    assert record["has_max_db"] is False
    assert record["autoupdate_disabled"] is True
