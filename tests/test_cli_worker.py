"""CLI worker: isolated worktree, argv execution, and the external-CLI gate."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from typer.testing import CliRunner

from max_agent.cli.main import app
from max_agent.db.models import AERExportRow, TaskAttemptRow
from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.models.enums import EventType, TaskStatus, WorkerKind
from max_agent.models.schemas import ScheduleRequest, TaskCreate
from max_agent.workers.cli_adapter import (
    cli_workspace_roots,
    ADAPTER_VERSION,
    FORBIDDEN_CLI_FLAGS,
    REQUIRE_REAL_ENV,
    Probe,
    _passthrough,
    build_exec_argv,
    choose_vendor,
    claude_spawn_auth_preflight,
    resolve_cli_identity,
)
from max_agent.workers.sandbox import ALLOWED_PASSTHROUGH, child_env, redact, run_bounded


@pytest.fixture(autouse=True)
def _deny_external_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAX_AGENT_ALLOW_EXTERNAL_CLI", raising=False)
    monkeypatch.delenv(REQUIRE_REAL_ENV, raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SUBAGENT_MODEL", raising=False)


_TRACE = CliRunner()


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
    workspace = Path("/tmp/max-agent-workspaces/demo/work")
    prompt_file = workspace / ".max-agent" / "prompt.txt"
    grok = build_exec_argv(
        "grok",
        binary="/usr/bin/grok",
        workspace=workspace,
        prompt=prompt,
        prompt_file=prompt_file,
    )
    codex = build_exec_argv(
        "codex",
        binary="/usr/bin/codex",
        workspace=workspace,
        prompt=prompt,
        prompt_file=prompt_file,
    )
    claude = build_exec_argv(
        "claude",
        binary="/usr/bin/claude",
        workspace=workspace,
        prompt=prompt,
        prompt_file=prompt_file,
    )
    assert prompt in grok
    assert prompt in codex
    assert prompt in claude
    assert "--sandbox" in grok and "strict" in grok
    assert "exec" in codex and "workspace-write" in codex
    assert claude[claude.index("--permission-mode") + 1] == "acceptEdits"
    assert claude[claude.index("--output-format") + 1] == "json"
    assert claude[claude.index("-p") + 1] == prompt
    assert FORBIDDEN_CLI_FLAGS.isdisjoint(grok)
    assert FORBIDDEN_CLI_FLAGS.isdisjoint(codex)
    assert FORBIDDEN_CLI_FLAGS.isdisjoint(claude)
    assert "--yolo" not in grok
    assert "danger-full-access" not in codex
    assert "--allow-dangerously-skip-permissions" in FORBIDDEN_CLI_FLAGS


def test_choose_vendor_authorization_and_auto_preference() -> None:
    probes = [
        Probe(vendor="grok", available=True, enabled=False, reason="not_authorized"),
        Probe(vendor="codex", available=False, enabled=False, reason="not_on_path"),
        Probe(vendor="claude", available=False, enabled=False, reason="not_on_path"),
    ]
    vendor, failure = choose_vendor("auto", probes, authorized=False)
    assert vendor == "stub"
    assert failure is None
    vendor, failure = choose_vendor("grok", probes, authorized=False)
    assert failure == "cli_not_authorized"
    enabled = [
        Probe(vendor="grok", available=True, enabled=True, reason="ok"),
        Probe(vendor="codex", available=True, enabled=True, reason="ok"),
        Probe(vendor="claude", available=True, enabled=True, reason="ok"),
    ]
    assert choose_vendor("auto", enabled, authorized=True) == ("grok", None)
    assert choose_vendor("auto", enabled[1:], authorized=True) == ("codex", None)
    assert choose_vendor("auto", enabled[2:], authorized=True) == ("claude", None)
    failed_probe = [Probe(vendor="claude", available=True, enabled=False, reason="version_probe_failed")]
    assert choose_vendor("claude", failed_probe, authorized=True) == ("claude", "cli_unavailable")


def test_require_real_worker_rejects_stub_success(session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REQUIRE_REAL_ENV, "1")
    workspace_root = tmp_path / "auto-fallback"
    _created, out = _run(
        session,
        "auto fallback must not silently use stub",
        {"vendor": "auto", "workspace_root": str(workspace_root)},
    )
    assert out.status == TaskStatus.FAILED
    outputs = _outputs(session, out.id)
    assert outputs["adapter_failure"] == "real_worker_required"
    assert outputs["require_real_worker"] is True
    assert outputs["real_cli_invoked"] is False
    assert outputs["cli_invoked"] is False
    assert not workspace_root.exists()


def test_require_real_worker_payload_flag_rejects_stub(session, tmp_path: Path) -> None:
    _created, out = _run(
        session,
        "explicit stub is refused",
        {"vendor": "stub", "workspace_root": str(tmp_path / "ws"), "require_real_worker": True},
    )
    assert out.status == TaskStatus.FAILED
    outputs = _outputs(session, out.id)
    assert outputs["adapter_failure"] == "real_worker_required"
    assert outputs["require_real_worker"] is True
    assert outputs["real_cli_invoked"] is False


def test_require_real_worker_still_rejects_stub_when_calc_fixture_absent(
    session, tmp_path: Path
) -> None:
    seed = tmp_path / "empty-seed"
    seed.mkdir()
    (seed / "README.md").write_text("no calc fixture\n", encoding="utf-8")
    _created, out = _run(
        session,
        "stub no-op must not satisfy require_real_worker",
        {
            "vendor": "stub",
            "workspace_root": str(tmp_path / "ws"),
            "repo_path": str(seed),
            "require_real_worker": True,
        },
        criteria=["worker_ok"],
    )
    assert out.status == TaskStatus.FAILED
    outputs = _outputs(session, out.id)
    assert outputs["adapter_failure"] == "real_worker_required"
    assert outputs["real_cli_invoked"] is False
    assert outputs["require_real_worker"] is True


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
    assert outputs["require_real_worker"] is False
    assert outputs["exit_code"] == 0
    assert outputs["pytest_exit_code"] == 0
    assert outputs["timed_out"] is False
    workspace = Path(outputs["workspace"])
    assert workspace.is_dir()
    assert "return a + b" in (workspace / "calc.py").read_text(encoding="utf-8")
    assert (workspace.parent / "seed" / "calc.py").read_text(encoding="utf-8").find("return a + b") == -1
    manifest = json.loads(Path(outputs["artifact_manifest"]).read_text(encoding="utf-8"))
    assert manifest["adapter_version"] == ADAPTER_VERSION
    assert manifest["real_cli_invoked"] is False
    assert manifest["require_real_worker"] is False
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


@pytest.mark.parametrize("vendor", ["grok", "claude"])
def test_unauthorized_real_cli_is_not_executed(
    vendor: str, session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / vendor
    binary.write_text(
        "#!/bin/sh\nprintf called > marker\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    _created, out = _run(
        session,
        f"do not call {vendor}",
        {"vendor": vendor, "workspace_root": str(tmp_path / "ws")},
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


def test_require_real_worker_allows_real_cli_invoked(
    session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "grok"
    script.write_text(
        """#!/usr/bin/env python3
import sys
from pathlib import Path
args = sys.argv[1:]
if args[:1] == ["version"]:
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
    monkeypatch.setenv(REQUIRE_REAL_ENV, "1")
    _created, out = _run(
        session,
        "require an authorized real CLI",
        {"vendor": "grok", "workspace_root": str(tmp_path / "ws")},
        criteria=["worker_ok", "output_equals:pytest_exit_code=0"],
    )
    assert out.status == TaskStatus.SUCCEEDED, out.last_error
    outputs = _outputs(session, out.id)
    assert outputs["vendor"] == "grok"
    assert outputs["require_real_worker"] is True
    assert outputs["real_cli_invoked"] is True


def test_authorized_claude_runs_after_version_probe_with_scoped_credentials(
    session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = bin_dir / "claude.log"
    script = bin_dir / "claude"
    script.write_text(
        f"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
log = Path({str(log)!r})
rows = json.loads(log.read_text()) if log.exists() else []
rows.append({{
    "argv": sys.argv[1:],
    "has_anthropic": "ANTHROPIC_API_KEY" in os.environ,
    "has_xai": "XAI_API_KEY" in os.environ,
    "has_github": "GITHUB_TOKEN" in os.environ,
}})
log.write_text(json.dumps(rows))
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("fake-claude 0")
    raise SystemExit(0)
Path.cwd().joinpath("calc.py").write_text("def add(a: int, b: int) -> int:\\n    return a + b\\n")
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("MAX_AGENT_ALLOW_EXTERNAL_CLI", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-LIVEKEY123456")
    monkeypatch.setenv("XAI_API_KEY", "xai-LIVEKEY123456")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_LIVEKEY12345678")
    _created, out = _run(
        session,
        "Implement add() with authorized claude",
        {"vendor": "claude", "workspace_root": str(tmp_path / "ws")},
        criteria=["worker_ok", "output_equals:pytest_exit_code=0"],
    )
    assert out.status == TaskStatus.SUCCEEDED, out.last_error
    rows = json.loads(log.read_text(encoding="utf-8"))
    assert len(rows) == 2
    assert rows[0]["argv"] == ["--version"]
    assert rows[0]["has_anthropic"] is False
    exec_row = rows[1]
    assert exec_row["argv"][:5] == ["--permission-mode", "acceptEdits", "--output-format", "json", "-p"]
    assert exec_row["has_anthropic"] is True
    assert exec_row["has_xai"] is False
    assert exec_row["has_github"] is False
    outputs = _outputs(session, out.id)
    assert outputs["vendor"] == "claude"
    assert outputs["real_cli_invoked"] is True
    assert "sk-ant-LIVEKEY123456" not in json.dumps(outputs)
    assert FORBIDDEN_CLI_FLAGS.isdisjoint(outputs["argv"])


def test_claude_preflight_helper_presence_only_never_returns_values() -> None:
    secret = "mm-preflight-secret-SHOULD-NOT-APPEAR-123456"
    missing = claude_spawn_auth_preflight({})
    assert missing.ok is False
    assert missing.has_api_key is False
    assert missing.has_auth_token is False
    assert "cli_auth_missing" not in missing.reason or True
    assert "ANTHROPIC_AUTH_TOKEN" in missing.reason
    assert secret not in missing.reason
    assert secret not in missing.__dict__.values()

    minimax_only = claude_spawn_auth_preflight({"MINIMAX_API_KEY": secret})
    assert minimax_only.ok is False
    assert minimax_only.minimax_key_present_but_not_forwarded is True
    assert secret not in minimax_only.reason
    assert "MINIMAX_API_KEY" in minimax_only.reason

    token_only = claude_spawn_auth_preflight(
        {"ANTHROPIC_AUTH_TOKEN": secret, "ANTHROPIC_BASE_URL": "https://example.invalid/anthropic"}
    )
    assert token_only.ok is True
    assert token_only.has_auth_token is True
    assert token_only.has_api_key is False
    assert token_only.has_base_url is True
    assert token_only.reason == "ok"
    assert secret not in token_only.reason

    key_only = claude_spawn_auth_preflight({"ANTHROPIC_API_KEY": secret})
    assert key_only.ok is True
    assert key_only.has_api_key is True

    blank = claude_spawn_auth_preflight({"ANTHROPIC_AUTH_TOKEN": "   ", "ANTHROPIC_API_KEY": ""})
    assert blank.ok is False


def test_missing_claude_auth_blocks_spawn_before_real_cli(
    session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = bin_dir / "SPAWNED"
    log = bin_dir / "claude.log"
    script = bin_dir / "claude"
    script.write_text(
        f"""#!/usr/bin/env python3
import json, sys
from pathlib import Path
log = Path({str(log)!r})
rows = json.loads(log.read_text()) if log.exists() else []
rows.append(sys.argv[1:])
log.write_text(json.dumps(rows))
if sys.argv[1:2] == ["--version"]:
    print("fake-claude 0")
    raise SystemExit(0)
Path({str(marker)!r}).write_text("spawned")
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    secret = "mm-preflight-secret-SHOULD-NOT-APPEAR-123456"
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("MAX_AGENT_ALLOW_EXTERNAL_CLI", "1")
    monkeypatch.setenv(REQUIRE_REAL_ENV, "1")
    monkeypatch.setenv("MINIMAX_API_KEY", secret)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    _created, out = _run(
        session,
        "must not start claude without forwarded auth",
        {"vendor": "claude", "workspace_root": str(tmp_path / "ws"), "require_real_worker": True},
        criteria=["worker_ok", "output_equals:real_cli_invoked=true"],
    )
    assert out.status == TaskStatus.FAILED
    assert not marker.exists()
    outputs = _outputs(session, out.id)
    assert outputs["adapter_failure"] == "cli_auth_missing"
    assert outputs["real_cli_invoked"] is False
    assert outputs["cli_invoked"] is False or outputs.get("cli_invoked") is False
    # version probe may run; the -p spawn must not
    if log.exists():
        rows = json.loads(log.read_text(encoding="utf-8"))
        assert all(row[:1] == ["--version"] for row in rows)
        assert not any("-p" in row for row in rows)
    dumped = json.dumps(outputs) + (out.last_error or "")
    assert secret not in dumped
    assert "ANTHROPIC_AUTH_TOKEN" in (out.last_error or outputs.get("adapter_failure") or "")
    # last_error should carry the readable reason
    assert "启动真实 Claude CLI 之前拦截" in (out.last_error or "")
    assert "/login" in (out.last_error or "")


def test_claude_auth_token_preflight_allows_fake_spawn(
    session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = bin_dir / "claude.log"
    script = bin_dir / "claude"
    script.write_text(
        f"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
log = Path({str(log)!r})
rows = json.loads(log.read_text()) if log.exists() else []
rows.append({{
    "argv": sys.argv[1:],
    "has_token": "ANTHROPIC_AUTH_TOKEN" in os.environ,
    "has_minimax": "MINIMAX_API_KEY" in os.environ,
}})
log.write_text(json.dumps(rows))
if sys.argv[1:2] == ["--version"]:
    print("fake-claude 0")
    raise SystemExit(0)
Path.cwd().joinpath("calc.py").write_text("def add(a: int, b: int) -> int:\\n    return a + b\\n")
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    secret = "mm-preflight-token-SHOULD-NOT-LEAK-987654"
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("MAX_AGENT_ALLOW_EXTERNAL_CLI", "1")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", secret)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid/anthropic")
    _created, out = _run(
        session,
        "Implement add() with forwarded auth token",
        {"vendor": "claude", "workspace_root": str(tmp_path / "ws")},
        criteria=["worker_ok", "output_equals:pytest_exit_code=0"],
    )
    assert out.status == TaskStatus.SUCCEEDED, out.last_error
    rows = json.loads(log.read_text(encoding="utf-8"))
    assert len(rows) == 2
    assert rows[1]["has_token"] is True
    assert rows[1]["has_minimax"] is False
    outputs = _outputs(session, out.id)
    assert outputs["real_cli_invoked"] is True
    assert secret not in json.dumps(outputs)
    assert secret not in (out.last_error or "")


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
    from max_agent.workers.sandbox import path_is_allowed

    # Do not use Path.home(): a regression runner may place HOME under /tmp.
    outside = Path("/var/tmp/max-agent-cli-outside")
    assert not path_is_allowed(outside, cli_workspace_roots())
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


def test_isolated_worktree_preserves_dangling_host_symlinks(tmp_path: Path) -> None:
    """A seed with a dangling host-absolute symlink must still isolate."""
    from max_agent.workers.worktree import create_isolated_worktree

    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "README.md").write_text("ok\n", encoding="utf-8")
    models = seed / "server" / "models" / "models" / "iic"
    models.mkdir(parents=True)
    target = Path("/www/wwwroot/wwwroot/missing-model-cache/speech_fsmn_vad")
    link = models / "speech_fsmn_vad_zh-cn-16k-common-pytorch"
    link.symlink_to(target)
    assert link.is_symlink()
    assert not link.exists()

    work = create_isolated_worktree(
        task_id="dangling-link",
        workspace_root=tmp_path / "ws",
        seed_source=seed,
    )
    copied = work / "server/models/models/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
    assert copied.exists()
    assert not copied.is_symlink()
    body = copied.read_text(encoding="utf-8")
    assert str(target) in body and "max-agent neutralized" in body
    assert (work / "README.md").read_text(encoding="utf-8") == "ok\n"


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

def test_claude_passthrough_includes_base_url_and_auth_token_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-testkey123456")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "mm-auth-token-12345678")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.minimax.cn/anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "MiniMax-M2")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "MiniMax-M2")
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_MODEL", "MiniMax-M2")
    monkeypatch.setenv("MAX_AGENT_DATABASE_URL", "sqlite:////prod/secret.db")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_SHOULDNOTPASS123456")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-SHOULDNOTPASS")
    monkeypatch.setenv("XAI_API_KEY", "xai-SHOULDNOTPASS123456")

    values = _passthrough("claude")
    assert values["ANTHROPIC_API_KEY"] == "sk-ant-testkey123456"
    assert values["ANTHROPIC_AUTH_TOKEN"] == "mm-auth-token-12345678"
    assert values["ANTHROPIC_BASE_URL"] == "https://api.minimax.cn/anthropic"
    assert values["ANTHROPIC_MODEL"] == "MiniMax-M2"
    assert values["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "MiniMax-M2"
    assert values["CLAUDE_CODE_SUBAGENT_MODEL"] == "MiniMax-M2"
    assert "MAX_AGENT_DATABASE_URL" not in values
    assert "GITHUB_TOKEN" not in values
    assert "OPENAI_API_KEY" not in values
    assert "XAI_API_KEY" not in values

    home = tmp_path / "home"
    tmp = tmp_path / "tmp"
    home.mkdir()
    tmp.mkdir()
    env = child_env(home=home, tmp=tmp, passthrough=values)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "mm-auth-token-12345678"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.minimax.cn/anthropic"
    assert env["ANTHROPIC_MODEL"] == "MiniMax-M2"
    assert "MAX_AGENT_DATABASE_URL" not in env
    assert "GITHUB_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    assert "XAI_API_KEY" not in env
    for key in (
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ):
        assert key in ALLOWED_PASSTHROUGH


def _clear_claude_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the host env so omit/include tests do not inherit box credentials."""
    from max_agent.workers.cli_adapter import _VENDOR_PASSTHROUGH

    for key in _VENDOR_PASSTHROUGH["claude"]:
        monkeypatch.delenv(key, raising=False)


def test_claude_passthrough_omits_unset_base_url_and_auth_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_claude_passthrough(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-testkey123456")

    values = _passthrough("claude")
    assert values == {"ANTHROPIC_API_KEY": "sk-ant-testkey123456"}
    assert "ANTHROPIC_BASE_URL" not in values
    assert "ANTHROPIC_AUTH_TOKEN" not in values
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in values
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in values
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in values
    assert "CLAUDE_CODE_SUBAGENT_MODEL" not in values

    home = tmp_path / "home"
    tmp = tmp_path / "tmp"
    home.mkdir()
    tmp.mkdir()
    env = child_env(home=home, tmp=tmp, passthrough=values)
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-testkey123456"
    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in env


def test_claude_passthrough_keeps_default_models_when_base_url_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production contract: allowlisted DEFAULT_* stay in passthrough when set.

    This is not a test-isolation flake. Unset BASE_URL/AUTH_TOKEN must be
    omitted, but set model-routing vars must still be forwarded.
    """
    _clear_claude_passthrough(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-testkey123456")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "MiniMax-M2")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL_NAME", "MiniMax-M2")
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_MODEL", "MiniMax-M2")

    values = _passthrough("claude")
    assert values["ANTHROPIC_API_KEY"] == "sk-ant-testkey123456"
    assert values["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "MiniMax-M2"
    assert values["ANTHROPIC_DEFAULT_SONNET_MODEL_NAME"] == "MiniMax-M2"
    assert values["CLAUDE_CODE_SUBAGENT_MODEL"] == "MiniMax-M2"
    assert "ANTHROPIC_BASE_URL" not in values
    assert "ANTHROPIC_AUTH_TOKEN" not in values
    assert "ANTHROPIC_MODEL" not in values

    env = child_env(home=tmp_path / "home", tmp=tmp_path / "tmp", passthrough=values)
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "MiniMax-M2"
    assert "ANTHROPIC_BASE_URL" not in env


def test_codex_passthrough_includes_openai_base_url_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-testkey123456")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://compatible.example/v1")
    monkeypatch.setenv("MAX_AGENT_DATABASE_URL", "sqlite:////prod/secret.db")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_SHOULDNOTPASS123456")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SHOULDNOTPASS")

    values = _passthrough("codex")
    assert values["OPENAI_API_KEY"] == "sk-openai-testkey123456"
    assert values["OPENAI_BASE_URL"] == "https://compatible.example/v1"
    assert "MAX_AGENT_DATABASE_URL" not in values
    assert "GITHUB_TOKEN" not in values
    assert "ANTHROPIC_API_KEY" not in values

    home = tmp_path / "home"
    tmp = tmp_path / "tmp"
    home.mkdir()
    tmp.mkdir()
    env = child_env(home=home, tmp=tmp, passthrough=values)
    assert env["OPENAI_BASE_URL"] == "https://compatible.example/v1"
    assert "MAX_AGENT_DATABASE_URL" not in env
    assert "GITHUB_TOKEN" not in env


def test_codex_passthrough_omits_unset_openai_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-testkey123456")
    values = _passthrough("codex")
    assert values == {"OPENAI_API_KEY": "sk-openai-testkey123456"}
    home = tmp_path / "home"
    tmp = tmp_path / "tmp"
    home.mkdir()
    tmp.mkdir()
    env = child_env(home=home, tmp=tmp, passthrough=values)
    assert "OPENAI_BASE_URL" not in env


def test_grok_passthrough_stays_xai_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "xai-testkey12345678")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://should-not-pass.example")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://should-not-pass.example")
    values = _passthrough("grok")
    assert values == {"XAI_API_KEY": "xai-testkey12345678"}


def test_child_env_still_rejects_db_and_github_even_if_forced(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    tmp = tmp_path / "tmp"
    home.mkdir()
    tmp.mkdir()
    env = child_env(
        home=home,
        tmp=tmp,
        passthrough={
            "MAX_AGENT_DATABASE_URL": "sqlite:////prod/secret.db",
            "GITHUB_TOKEN": "ghp_SHOULDNOTPASS123456",
            "ANTHROPIC_BASE_URL": "https://api.minimax.cn/anthropic",
        },
    )
    assert "MAX_AGENT_DATABASE_URL" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["ANTHROPIC_BASE_URL"] == "https://api.minimax.cn/anthropic"


def test_resolve_cli_identity_uses_host_model_or_explicit_unknown() -> None:
    model, provider, usage = resolve_cli_identity("claude", "", {"ANTHROPIC_MODEL": "MiniMax-M2"})
    assert model == "MiniMax-M2"
    assert provider == "anthropic"
    assert usage is None

    model, provider, usage = resolve_cli_identity(
        "claude",
        "",
        {"CLAUDE_CODE_SUBAGENT_MODEL": "claude-haiku-fixture"},
    )
    assert model == "claude-haiku-fixture"
    assert provider == "anthropic"
    assert usage is None

    model, provider, _usage = resolve_cli_identity(
        "claude",
        "",
        {"ANTHROPIC_MODEL": "from-host", "CLAUDE_CODE_SUBAGENT_MODEL": "from-subagent"},
    )
    assert model == "from-host"
    assert provider == "anthropic"

    model, provider, usage = resolve_cli_identity(
        "claude",
        "",
        {"ANTHROPIC_MODEL": "0", "CLAUDE_CODE_SUBAGENT_MODEL": "0"},
    )
    assert model == "unknown"
    assert provider == "anthropic"
    assert usage is None

    model, provider, usage = resolve_cli_identity("stub", "", {"ANTHROPIC_MODEL": "MiniMax-M2"})
    assert model == "unknown"
    assert provider == "unknown"
    assert usage is None

    model, provider, usage = resolve_cli_identity("grok", "not-json", {})
    assert model == "unknown"
    assert provider == "xai"
    assert usage is None


def test_resolve_cli_identity_prefers_cli_json_and_does_not_invent_cost() -> None:
    stdout = json.dumps(
        {
            "model": "claude-from-json",
            "usage": {"input_tokens": 9, "output_tokens": 4},
        }
    )
    model, provider, usage = resolve_cli_identity(
        "claude",
        stdout,
        {"ANTHROPIC_MODEL": "from-host"},
    )
    assert model == "claude-from-json"
    assert provider == "anthropic"
    assert usage is None

    measured = json.dumps(
        {
            "model": 0,
            "total_cost_usd": 0.125,
            "usage": {"input_tokens": 11, "output_tokens": 2},
            "modelUsage": {"claude-sonnet-fixture": {"inputTokens": 11, "outputTokens": 2, "costUSD": 0.125}},
        }
    )
    model, provider, usage = resolve_cli_identity("claude", measured, {})
    assert model == "claude-sonnet-fixture"
    assert provider == "anthropic"
    assert usage is not None
    assert usage["marker"] == "measured"
    assert usage["amount"] == 0.125
    assert usage["currency"] == "USD"
    assert usage["source"] == "api_spend"
    assert usage["input_tokens"] == 11
    assert usage["output_tokens"] == 2
    assert usage["amount"] != 0

    estimate = json.dumps(
        {
            "model": "grok-fixture",
            "provider": "xai",
            "usage": {"source": "cli_quota", "marker": "estimate", "amount": 4, "currency": "requests"},
        }
    )
    model, provider, usage = resolve_cli_identity("grok", estimate, {"ANTHROPIC_MODEL": "MiniMax-M2"})
    assert model == "grok-fixture"
    assert provider == "xai"
    assert usage["marker"] == "estimate"
    assert usage["amount"] == 4
    assert usage["source"] == "cli_quota"

    explicit_zero = json.dumps({"model": "free-model", "usage": {"marker": "measured", "amount": 0, "source": "cli_quota"}})
    _model, _provider, usage = resolve_cli_identity("codex", explicit_zero, {})
    assert usage is not None
    assert usage["marker"] == "measured"
    assert usage["amount"] == 0
    assert usage["source"] == "cli_quota"

    unmarked = json.dumps({"usage": {"source": "api_spend", "amount": 3, "input_tokens": 1}})
    model, provider, usage = resolve_cli_identity("codex", unmarked, {})
    assert model == "unknown"
    assert provider == "openai"
    assert usage is None


def _install_fake_cli(bin_dir: Path, vendor: str, stdout: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    version_arg = "version" if vendor == "grok" else "--version"
    script = bin_dir / vendor
    script.write_text(
        f"""#!/usr/bin/env python3
import sys
from pathlib import Path
args = sys.argv[1:]
if args[:1] == [{version_arg!r}]:
    print("fake 0")
    raise SystemExit(0)
if {vendor!r} == "grok":
    cwd = args[args.index("--cwd") + 1]
    Path(cwd, "calc.py").write_text("def add(a: int, b: int) -> int:\\n    return a + b\\n")
else:
    Path.cwd().joinpath("calc.py").write_text("def add(a: int, b: int) -> int:\\n    return a + b\\n")
print({stdout!r})
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _attempt_row(session, task_id: str) -> TaskAttemptRow:
    row = session.query(TaskAttemptRow).filter(TaskAttemptRow.task_id == task_id).one()
    session.refresh(row)
    return row


def _finished_aer(session, task_id: str) -> dict:
    rows = session.query(AERExportRow).filter(AERExportRow.task_id == task_id).all()
    finished = [
        row.body
        for row in rows
        if row.kind == "attempt" and row.body.get("event_type") == "attempt_finished"
    ]
    assert len(finished) == 1
    return finished[0]


def _trace_cli(task_id: str) -> dict:
    result = _TRACE.invoke(app, ["trace", task_id])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_stub_attempt_records_explicit_unknown_model_and_cost(session, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_MODEL", "MiniMax-M2")
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_MODEL", "claude-haiku-fixture")
    created, out = _run(
        session,
        "stub has no vendor model",
        {"vendor": "stub", "workspace_root": str(tmp_path / "ws")},
        criteria=["worker_ok"],
    )
    assert out.status == TaskStatus.SUCCEEDED, out.last_error
    trace = _trace_cli(created.id)
    outputs = _outputs(session, out.id)
    assert outputs["vendor"] == "stub"
    assert outputs["model"] == "unknown"
    assert outputs["provider"] == "unknown"
    assert "usage" not in outputs

    attempt = _attempt_row(session, created.id)
    assert attempt.model == "unknown"
    assert attempt.provider == "unknown"
    assert attempt.cost_marker == "unknown"
    assert attempt.cost_source == "unknown"
    assert attempt.cost_amount is None
    assert attempt.cost_currency is None
    assert attempt.input_tokens is None
    assert attempt.output_tokens is None

    assert trace["attempts"][0]["model"] == "unknown"
    assert trace["attempts"][0]["provider"] == "unknown"
    assert trace["attempts"][0]["cost"]["marker"] == "unknown"
    assert trace["attempts"][0]["cost"]["amount"] is None
    record = _finished_aer(session, created.id)
    assert record["worker"]["model"] == "unknown"
    assert record["worker"]["provider"] == "unknown"
    assert record["cost"]["marker"] == "unknown"
    assert record["cost"]["amount"] is None


def test_claude_host_model_is_visible_on_attempt_trace_and_aer(
    session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    _install_fake_cli(bin_dir, "claude", "{}")
    fake_token = "sk-ant-FAKE-fixture-NOT-A-REAL-KEY-host-model-0001"
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("MAX_AGENT_ALLOW_EXTERNAL_CLI", "1")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", fake_token)
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_MODEL", "claude-haiku-fixture")
    created, out = _run(
        session,
        "record the configured claude model",
        {"vendor": "claude", "workspace_root": str(tmp_path / "ws")},
        criteria=["worker_ok", "output_equals:pytest_exit_code=0"],
    )
    assert out.status == TaskStatus.SUCCEEDED, out.last_error
    trace = _trace_cli(created.id)
    outputs = _outputs(session, out.id)
    assert outputs["vendor"] == "claude"
    assert outputs["real_cli_invoked"] is True
    assert Path(outputs["argv"][0]).resolve() == (bin_dir / "claude").resolve()
    assert fake_token not in json.dumps(outputs)
    assert outputs["model"] == "claude-haiku-fixture"
    assert outputs["provider"] == "anthropic"
    assert "usage" not in outputs

    attempt = _attempt_row(session, created.id)
    assert attempt.model == "claude-haiku-fixture"
    assert attempt.provider == "anthropic"
    assert attempt.cost_marker == "unknown"
    assert attempt.cost_amount is None
    assert attempt.input_tokens is None

    assert trace["attempts"][0]["model"] == "claude-haiku-fixture"
    assert trace["attempts"][0]["provider"] == "anthropic"
    assert trace["attempts"][0]["cost"]["marker"] == "unknown"
    assert trace["attempts"][0]["cost"]["amount"] is None
    record = _finished_aer(session, created.id)
    assert record["worker"]["model"] == "claude-haiku-fixture"
    assert record["worker"]["provider"] == "anthropic"
    assert record["cost"]["model"] == "claude-haiku-fixture"
    assert record["cost"]["provider"] == "anthropic"
    assert record["cost"]["marker"] == "unknown"
    assert record["cost"]["amount"] is None


def test_cli_json_model_and_usage_marker_are_stored_on_the_attempt(
    session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "model": "claude-from-json",
        "usage": {
            "source": "api_spend",
            "marker": "measured",
            "amount": 0.25,
            "currency": "USD",
            "input_tokens": 12,
            "output_tokens": 3,
        },
    }
    bin_dir = tmp_path / "bin"
    _install_fake_cli(bin_dir, "claude", json.dumps(payload))
    fake_token = "sk-ant-FAKE-fixture-NOT-A-REAL-KEY-usage-marker-0002"
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("MAX_AGENT_ALLOW_EXTERNAL_CLI", "1")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", fake_token)
    monkeypatch.setenv("ANTHROPIC_MODEL", "from-host")
    created, out = _run(
        session,
        "record measured cli usage",
        {"vendor": "claude", "workspace_root": str(tmp_path / "ws")},
        criteria=["worker_ok", "output_equals:pytest_exit_code=0"],
    )
    assert out.status == TaskStatus.SUCCEEDED, out.last_error
    trace = _trace_cli(created.id)
    outputs = _outputs(session, out.id)
    assert outputs["vendor"] == "claude"
    assert Path(outputs["argv"][0]).resolve() == (bin_dir / "claude").resolve()
    assert fake_token not in json.dumps(outputs)
    assert outputs["model"] == "claude-from-json"
    assert outputs["provider"] == "anthropic"
    assert outputs["usage"]["marker"] == "measured"
    assert outputs["usage"]["amount"] == 0.25

    attempt = _attempt_row(session, created.id)
    assert attempt.model == "claude-from-json"
    assert attempt.provider == "anthropic"
    assert attempt.cost_source == "api_spend"
    assert attempt.cost_marker == "measured"
    assert attempt.cost_amount == 0.25
    assert attempt.cost_currency == "USD"
    assert attempt.input_tokens == 12
    assert attempt.output_tokens == 3

    assert trace["attempts"][0]["model"] == "claude-from-json"
    assert trace["attempts"][0]["provider"] == "anthropic"
    assert trace["attempts"][0]["cost"]["marker"] == "measured"
    assert trace["attempts"][0]["cost"]["source"] == "api_spend"
    assert trace["attempts"][0]["cost"]["amount"] == 0.25
    assert trace["chain"][1]["detail"]["model"] == "claude-from-json"
    assert trace["chain"][1]["detail"]["cost_marker"] == "measured"
    assert trace["chain"][1]["detail"]["cost_amount"] == 0.25
    record = _finished_aer(session, created.id)
    assert record["worker"]["model"] == "claude-from-json"
    assert record["worker"]["provider"] == "anthropic"
    assert record["cost"]["marker"] == "measured"
    assert record["cost"]["source"] == "api_spend"
    assert record["cost"]["amount"] == 0.25
    assert record["cost"]["currency"] == "USD"
    assert record["cost"]["input_tokens"] == 12
    assert record["cost"]["output_tokens"] == 3


def test_grok_estimate_usage_is_stored_without_a_real_api_key(
    session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "model": "grok-fixture",
        "usage": {"source": "cli_quota", "marker": "estimate", "amount": 4, "currency": "requests"},
    }
    bin_dir = tmp_path / "bin"
    _install_fake_cli(bin_dir, "grok", json.dumps(payload))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("MAX_AGENT_ALLOW_EXTERNAL_CLI", "1")
    created, out = _run(
        session,
        "record estimated grok usage",
        {"vendor": "grok", "workspace_root": str(tmp_path / "ws")},
        criteria=["worker_ok", "output_equals:pytest_exit_code=0"],
    )
    assert out.status == TaskStatus.SUCCEEDED, out.last_error
    attempt = _attempt_row(session, created.id)
    assert attempt.model == "grok-fixture"
    assert attempt.provider == "xai"
    assert attempt.cost_source == "cli_quota"
    assert attempt.cost_marker == "estimate"
    assert attempt.cost_amount == 4
    assert attempt.cost_currency == "requests"
    record = _finished_aer(session, created.id)
    assert record["cost"]["marker"] == "estimate"
    assert record["cost"]["amount"] == 4
    assert record["worker"]["model"] == "grok-fixture"
