"""CLI worker: isolated worktree, argv execution, and the external-CLI gate."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.models.enums import EventType, TaskStatus, WorkerKind
from max_agent.models.schemas import ScheduleRequest, TaskCreate
from max_agent.workers.cli_adapter import (
    ADAPTER_VERSION,
    FORBIDDEN_CLI_FLAGS,
    Probe,
    build_exec_argv,
    choose_vendor,
)
from max_agent.workers.sandbox import redact, run_bounded


@pytest.fixture(autouse=True)
def _deny_external_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAX_AGENT_ALLOW_EXTERNAL_CLI", raising=False)


def _outputs(session, task_id: str) -> dict:
    events = TaskService(session).list_events(task_id, limit=500)
    for event in reversed(events):
        if event.event_type == EventType.WORKER_OUTPUT:
            return dict(event.payload.get("outputs") or {})
    raise AssertionError("worker output was not journaled")


def _run(session, goal: str, payload: dict, **kwargs) -> object:
    criteria = kwargs.pop("criteria", ["worker_ok", "output_key:adapter_version"])
    timeout = kwargs.pop("timeout_seconds", 60)
    kind = kwargs.pop("worker_kind", WorkerKind.CLI)
    tags = kwargs.pop("tags", [])
    schedule_kind = kwargs.pop("schedule_kind", None)
    svc = TaskService(session)
    created = svc.create(
        TaskCreate(
            goal=goal,
            acceptance_criteria=criteria,
            worker_kind=kind,
            worker_payload=payload,
            timeout_seconds=timeout,
            tags=tags,
        )
    )
    req = ScheduleRequest(worker_kind=schedule_kind) if schedule_kind else None
    return created, Scheduler(session).run_one(created.id, req)


def test_redact_strips_token_shapes_and_known_secret_values() -> None:
    text = "token=ghp_ABCDEFGHIJKLMNOP secret sk-abcdefghij123 and supersecretvalue"
    cleaned = redact(text, ["supersecretvalue"])
    assert "ghp_" not in cleaned
    assert "sk-" not in cleaned
    assert "supersecretvalue" not in cleaned
    assert "[REDACTED]" in cleaned


def test_run_bounded_rejects_shell_strings_and_does_not_expand_metacharacters(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    with pytest.raises(TypeError):
        run_bounded(
            "echo hi",  # type: ignore[arg-type]
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            timeout_seconds=2,
        )
    result = run_bounded(
        ["/bin/echo", f"$(touch {marker})"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        timeout_seconds=2,
    )
    assert result.exit_code == 0
    assert not marker.exists()
    assert "$(touch" in result.stdout


def test_run_bounded_timeout_and_cancel_stop_the_process_group(tmp_path: Path) -> None:
    slept = run_bounded(
        ["python3", "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        timeout_seconds=0.4,
    )
    assert slept.timed_out is True
    assert slept.exit_code is None or slept.exit_code != 0

    cancel = tmp_path / "cancel"

    def _arm() -> None:
        time.sleep(0.2)
        cancel.write_text("stop", encoding="utf-8")

    threading.Thread(target=_arm, daemon=True).start()
    cancelled = run_bounded(
        ["python3", "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        timeout_seconds=10,
        cancel_file=cancel,
    )
    assert cancelled.cancelled is True


def test_exec_argv_keeps_prompt_as_one_argument_and_refuses_yolo() -> None:
    prompt = 'implement add; $(touch /tmp/pwned)'
    grok = build_exec_argv(
        "grok",
        binary="/usr/bin/grok",
        workspace=Path("/tmp/max-agent-workspaces/demo/work"),
        prompt=prompt,
        prompt_file=Path("/tmp/max-agent-workspaces/demo/work/.max-agent/prompt.txt"),
    )
    codex = build_exec_argv(
        "codex",
        binary="/usr/bin/codex",
        workspace=Path("/tmp/max-agent-workspaces/demo/work"),
        prompt=prompt,
        prompt_file=Path("/tmp/max-agent-workspaces/demo/work/.max-agent/prompt.txt"),
    )
    assert prompt in grok
    assert prompt in codex
    assert "--sandbox" in grok and "strict" in grok
    assert "exec" in codex and "workspace-write" in codex
    assert FORBIDDEN_CLI_FLAGS.isdisjoint(grok)
    assert FORBIDDEN_CLI_FLAGS.isdisjoint(codex)
    assert "--yolo" not in grok
    assert "danger-full-access" not in codex


def test_choose_vendor_skips_real_cli_without_authorization() -> None:
    probes = [
        Probe(vendor="grok", available=True, enabled=False, reason="not_authorized"),
        Probe(vendor="codex", available=False, enabled=False, reason="not_on_path"),
    ]
    vendor, failure = choose_vendor("auto", probes, authorized=False)
    assert vendor == "stub"
    assert failure is None
    vendor, failure = choose_vendor("grok", probes, authorized=False)
    assert failure == "cli_not_authorized"
    enabled = [
        Probe(vendor="grok", available=True, enabled=True, reason="ok"),
        Probe(vendor="codex", available=True, enabled=True, reason="ok"),
    ]
    vendor, failure = choose_vendor("auto", enabled, authorized=True)
    assert vendor == "grok"
    assert failure is None


def test_fixture_issue_passes_with_stub_and_archives_artifacts(session, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_SHOULDNOTLEAK1234")
    monkeypatch.setenv("XAI_API_KEY", "xai-SHOULDNOTLEAK1234")
    _created, out = _run(
        session,
        "Implement add() for fixture issue CLI-4",
        {
            "vendor": "auto",
            "workspace_root": str(tmp_path),
            "stub_canary": "ghp_SHOULDNOTLEAK1234",
        },
        criteria=["worker_ok", "output_key:adapter_version", "output_equals:pytest_exit_code=0"],
    )
    assert out.status == TaskStatus.SUCCEEDED, out.last_error
    outputs = _outputs(session, out.id)
    assert outputs["adapter_version"] == ADAPTER_VERSION
    assert outputs["vendor"] == "stub"
    assert outputs["cli_invoked"] is True
    assert outputs["real_cli_invoked"] is False
    assert outputs["exit_code"] == 0
    assert outputs["pytest_exit_code"] == 0
    assert outputs["timed_out"] is False
    workspace = Path(outputs["workspace"])
    assert workspace.is_dir()
    assert "return a + b" in (workspace / "calc.py").read_text(encoding="utf-8")
    assert (workspace.parent / "seed" / "calc.py").read_text(encoding="utf-8").find("return a + b") == -1
    manifest = json.loads(Path(outputs["artifact_manifest"]).read_text(encoding="utf-8"))
    assert manifest["adapter_version"] == ADAPTER_VERSION
    names = {item["path"] for item in manifest["artifacts"]}
    assert "calc.py" in names
    assert ".git" not in names
    blob = json.dumps(outputs)
    assert "ghp_SHOULDNOTLEAK1234" not in blob
    assert "xai-SHOULDNOTLEAK1234" not in blob
    keys = json.loads((workspace / ".max-agent" / "child-env-keys.json").read_text(encoding="utf-8"))
    assert "GITHUB_TOKEN" not in keys
    assert "XAI_API_KEY" not in keys
    assert "MAX_AGENT_DATABASE_URL" not in keys


def test_switching_worker_keeps_task_identity(session, tmp_path: Path) -> None:
    created, out = _run(
        session,
        "preserve-me",
        {"workspace_root": str(tmp_path), "answer": "ok"},
        worker_kind=WorkerKind.MOCK,
        schedule_kind=WorkerKind.CLI,
        criteria=["worker_ok"],
    )
    assert out.id == created.id
    assert out.goal == "preserve-me"
    assert out.attempts == 1
    assert out.status == TaskStatus.SUCCEEDED, out.last_error
    assert out.worker_kind == WorkerKind.CLI
    events = TaskService(session).list_events(out.id)
    created_event = next(event for event in events if event.event_type == EventType.CREATED)
    assert created_event.payload["worker_kind"] == "mock"


def test_unauthorized_grok_is_not_executed(session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "grok"
    binary.write_text(
        "#!/bin/sh\nprintf grok > marker\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    _created, out = _run(
        session,
        "do not call grok",
        {"vendor": "grok", "workspace_root": str(tmp_path / "ws")},
        criteria=["worker_ok"],
    )
    assert out.status == TaskStatus.FAILED
    outputs = _outputs(session, out.id)
    assert outputs["adapter_failure"] == "cli_not_authorized"
    assert outputs["real_cli_invoked"] is False
    assert not (bin_dir / "marker").exists()
    assert not (tmp_path / "marker").exists()


def test_authorized_grok_runs_only_after_version_probe(
    session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = bin_dir / "grok.log"
    script = bin_dir / "grok"
    script.write_text(
        f"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
log = Path({str(log)!r})
rows = json.loads(log.read_text()) if log.exists() else []
rows.append({{"argv": sys.argv[1:], "has_xai": "XAI_API_KEY" in os.environ, "has_github": "GITHUB_TOKEN" in os.environ}})
log.write_text(json.dumps(rows))
args = sys.argv[1:]
if args[:1] == ["version"]:
    print("fake-grok 0")
    raise SystemExit(0)
cwd = args[args.index("--cwd") + 1]
Path(cwd, "calc.py").write_text("def add(a: int, b: int) -> int:\\n    return a + b\\n")
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("MAX_AGENT_ALLOW_EXTERNAL_CLI", "1")
    monkeypatch.setenv("XAI_API_KEY", "xai-LIVEKEY123456")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_LIVEKEY12345678")
    _created, out = _run(
        session,
        "Implement add() with authorized grok",
        {"vendor": "auto", "workspace_root": str(tmp_path / "ws")},
        criteria=["worker_ok", "output_equals:pytest_exit_code=0"],
    )
    assert out.status == TaskStatus.SUCCEEDED, out.last_error
    rows = json.loads(log.read_text(encoding="utf-8"))
    assert rows[0]["argv"][:1] == ["version"]
    assert rows[0]["has_xai"] is False
    assert any("--sandbox" in row["argv"] and "strict" in row["argv"] for row in rows[1:])
    exec_row = rows[-1]
    assert exec_row["has_xai"] is True
    assert exec_row["has_github"] is False
    outputs = _outputs(session, out.id)
    assert outputs["vendor"] == "grok"
    assert outputs["real_cli_invoked"] is True
    assert "xai-LIVEKEY123456" not in json.dumps(outputs)
    assert FORBIDDEN_CLI_FLAGS.isdisjoint(outputs["argv"])


def test_shell_metacharacters_in_the_goal_stay_data(session, tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    goal = f"$(touch {marker}); rm -rf /"
    _created, out = _run(
        session,
        goal,
        {"vendor": "stub", "workspace_root": str(tmp_path / "ws")},
        criteria=["worker_ok"],
    )
    assert out.status == TaskStatus.SUCCEEDED, out.last_error
    assert not marker.exists()
    outputs = _outputs(session, out.id)
    assert all("$(touch" not in arg for arg in outputs["argv"])


def test_timeout_cancel_and_nonzero_exit_are_classifiable(session, tmp_path: Path) -> None:
    _created, timed = _run(
        session,
        "time out",
        {
            "vendor": "stub",
            "workspace_root": str(tmp_path / "timeout"),
            "stub_sleep_seconds": 30,
            "subprocess_timeout_seconds": 0.4,
        },
        criteria=["worker_ok"],
        timeout_seconds=30,
    )
    assert timed.status != TaskStatus.SUCCEEDED
    timeout_outputs = _outputs(session, timed.id)
    assert timeout_outputs["adapter_failure"] == "timeout"
    assert timeout_outputs["timed_out"] is True
    assert Path(timeout_outputs["artifact_manifest"]).is_file()

    cancel = tmp_path / "cancel-flag"

    def _arm() -> None:
        time.sleep(0.2)
        cancel.write_text("1", encoding="utf-8")

    threading.Thread(target=_arm, daemon=True).start()
    _created, cancelled = _run(
        session,
        "cancel me",
        {
            "vendor": "stub",
            "workspace_root": str(tmp_path / "cancel"),
            "stub_sleep_seconds": 30,
            "cancel_file": str(cancel),
        },
        criteria=["worker_ok"],
        timeout_seconds=30,
    )
    assert cancelled.status != TaskStatus.SUCCEEDED
    assert _outputs(session, cancelled.id)["adapter_failure"] == "cancelled"

    _created, failed = _run(
        session,
        "fail closed",
        {
            "vendor": "stub",
            "workspace_root": str(tmp_path / "fail"),
            "stub_exit_code": 2,
            "stub_canary": "ghp_FAILSECRET12345",
        },
        criteria=["worker_ok"],
    )
    assert failed.status != TaskStatus.SUCCEEDED
    failed_outputs = _outputs(session, failed.id)
    assert failed_outputs["adapter_failure"] == "nonzero_exit"
    assert failed_outputs["exit_code"] == 2
    assert "ghp_FAILSECRET12345" not in json.dumps(failed_outputs)
    manifest = json.loads(Path(failed_outputs["artifact_manifest"]).read_text(encoding="utf-8"))
    assert manifest["exit_code"] == 2


def test_output_is_capped(session, tmp_path: Path) -> None:
    _created, out = _run(
        session,
        "cap output",
        {
            "vendor": "stub",
            "workspace_root": str(tmp_path / "cap"),
            "stub_stdout_bytes": 100_000,
            "max_output_bytes": 128,
        },
        criteria=["worker_ok"],
    )
    assert out.status == TaskStatus.SUCCEEDED, out.last_error
    outputs = _outputs(session, out.id)
    assert outputs["output_truncated"] is True
    assert len(outputs["stdout"]) < 200


def test_workspace_outside_allowlist_is_refused(session, tmp_path: Path) -> None:
    outside = Path.home() / "max-agent-cli-outside"
    _created, out = _run(
        session,
        "escape",
        {"vendor": "stub", "workspace_root": str(outside)},
        criteria=["worker_ok"],
    )
    assert out.status == TaskStatus.FAILED
    assert _outputs(session, out.id)["adapter_failure"] == "path_violation"
    assert not outside.exists()


def test_source_repo_is_copied_not_edited(session, tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "calc.py").write_text(
        "def add(a: int, b: int) -> int:\n    raise NotImplementedError('fixture issue: implement add')\n",
        encoding="utf-8",
    )
    (origin / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (origin / ".env").write_text("SECRET=supersecretvalue\n", encoding="utf-8")
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "max-agent"
    env["GIT_AUTHOR_EMAIL"] = "max-agent@localhost"
    env["GIT_COMMITTER_NAME"] = "max-agent"
    env["GIT_COMMITTER_EMAIL"] = "max-agent@localhost"
    import subprocess

    subprocess.run(["git", "init", "-b", "main"], cwd=origin, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=origin, check=True, env=env)
    subprocess.run(["git", "commit", "-m", "origin"], cwd=origin, check=True, env=env)
    before = subprocess.run(["git", "worktree", "list"], cwd=origin, check=True, capture_output=True, text=True, env=env)
    _created, out = _run(
        session,
        "edit only the copy",
        {"vendor": "stub", "workspace_root": str(tmp_path / "ws"), "repo_path": str(origin)},
        criteria=["worker_ok"],
    )
    assert out.status == TaskStatus.SUCCEEDED, out.last_error
    assert "NotImplementedError" in (origin / "calc.py").read_text(encoding="utf-8")
    after = subprocess.run(["git", "worktree", "list"], cwd=origin, check=True, capture_output=True, text=True, env=env)
    assert before.stdout == after.stdout
    workspace = Path(_outputs(session, out.id)["workspace"])
    assert not (workspace / ".env").exists()
    assert "supersecretvalue" not in json.dumps(_outputs(session, out.id))


def test_production_tag_still_blocks_before_the_worker(session, tmp_path: Path) -> None:
    _created, out = _run(
        session,
        "do not run",
        {"vendor": "stub", "workspace_root": str(tmp_path / "blocked")},
        tags=["production"],
        criteria=["worker_ok"],
    )
    assert out.status == TaskStatus.WAITING_APPROVAL
    assert not (tmp_path / "blocked").exists()
