"""Kernel timeout must kill start_new_session children and keep worker outputs.

No real coding CLI is started here. The fixture is a local Python sleeper.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from max_agent.kernel.execution import execute_claim
from max_agent.kernel.lease import AttemptClaim
from max_agent.models.enums import FailureClass
from max_agent.models.schemas import WorkerResult
from max_agent.workers.base import Worker, WorkerContext
from max_agent.workers.cli_adapter import _cli_timeout
from max_agent.workers.sandbox import run_bounded
from max_agent.workers.worktree import create_isolated_worktree


def _claim(**kwargs) -> AttemptClaim:
    now = datetime.now(timezone.utc)
    values = dict(
        task_id="task-timeout-cleanup",
        attempt_id="attempt-timeout-cleanup",
        attempt_no=1,
        fencing_token=1,
        lease_owner="test-worker",
        lease_expires_at=now + timedelta(minutes=5),
        goal="hold a session-child until kernel timeout",
        acceptance_criteria=(),
        payload={},
        timeout_seconds=1,
        worker_kind="cli",
        has_side_effects=False,
    )
    values.update(kwargs)
    return AttemptClaim(**values)


def _session_factory():
    raise AssertionError("session factory should not be used by this fixture worker")


class _SessionChildWorker:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.pid_file = tmp_path / "child.pid"

    def run(self, ctx: WorkerContext) -> WorkerResult:
        script = r"""
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(sys.argv[1], "w", encoding="utf-8").write(str(os.getpid()))
time.sleep(30)
"""
        ran = run_bounded(
            [sys.executable, "-c", script, str(self.pid_file)],
            cwd=self.tmp_path,
            env={"PATH": os.environ.get("PATH", "/usr/bin"), "LANG": "C.UTF-8"},
            timeout_seconds=30,
        )
        return WorkerResult(
            ok=not ran.timed_out,
            summary="sleeper finished",
            outputs={
                "stdout": ran.stdout,
                "stderr": ran.stderr,
                "timed_out": ran.timed_out,
                "cancelled": ran.cancelled,
                "exit_code": ran.exit_code,
                "marker": "sleeper-result",
            },
        )


def _child_alive(pid_file: Path) -> bool:
    if not pid_file.exists():
        return False
    raw = pid_file.read_text(encoding="utf-8").strip()
    if not raw.isdigit():
        return False
    pid = int(raw)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_execute_claim_timeout_kills_session_child_and_keeps_outputs(tmp_path: Path) -> None:
    worker = _SessionChildWorker(tmp_path)
    claim = _claim(timeout_seconds=1)
    started = time.monotonic()
    result, failure = execute_claim(
        claim,
        session_factory=_session_factory,
        worker_resolver=lambda _kind: worker,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 8, elapsed
    assert failure == FailureClass.TIMEOUT
    assert result.ok is False
    assert result.outputs, "timeout must persist worker outputs, not an empty dict"
    assert result.outputs.get("timed_out") is True or result.outputs.get("kernel_timeout") is True
    assert result.outputs.get("marker") == "sleeper-result" or "adapter_failure" in result.outputs
    # Give a moment in case kill is asynchronous, then the sleeper must be gone.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and _child_alive(worker.pid_file):
        time.sleep(0.05)
    assert not _child_alive(worker.pid_file), "orphaned start_new_session child must be killed"


def test_cli_timeout_reserves_headroom_when_matching_kernel_budget() -> None:
    class _Ctx:
        timeout_seconds = 900

    chosen = _cli_timeout(_Ctx(), {})
    assert 60 <= chosen <= 870
    capped = _cli_timeout(_Ctx(), {"subprocess_timeout_seconds": 0.4})
    assert capped == 0.4


def test_worktree_neutralizes_absolute_host_symlinks(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "README.md").write_text("ok\n", encoding="utf-8")
    models = seed / "server" / "models" / "models" / "iic"
    models.mkdir(parents=True)
    target = Path("/www/wwwroot/wwwroot/missing-model-cache/speech_fsmn_vad")
    link = models / "speech_fsmn_vad_zh-cn-16k-common-pytorch"
    link.symlink_to(target)
    work = create_isolated_worktree(
        task_id="neutralize-link",
        workspace_root=tmp_path / "ws",
        seed_source=seed,
    )
    copied = work / "server/models/models/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
    assert copied.exists()
    assert not copied.is_symlink()
    text = copied.read_text(encoding="utf-8")
    assert "target=" in text and str(target) in text
    assert "max-agent neutralized" in text
    # The host path must not be created or traversed by the copy.
    assert not target.exists()


def test_worktree_skips_nohup_out(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "README.md").write_text("ok\n", encoding="utf-8")
    (seed / "nohup.out").write_text("log-noise\n", encoding="utf-8")
    work = create_isolated_worktree(
        task_id="skip-nohup",
        workspace_root=tmp_path / "ws",
        seed_source=seed,
    )
    assert (work / "README.md").exists()
    assert not (work / "nohup.out").exists()


class _SetsidGrandchildWorker:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.parent_pid_file = tmp_path / "parent.pid"
        self.child_pid_file = tmp_path / "grandchild.pid"

    def run(self, ctx: WorkerContext) -> WorkerResult:
        script = r"""
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
parent_pid_file, child_pid_file = sys.argv[1], sys.argv[2]
pid = os.fork()
if pid == 0:
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    open(child_pid_file, "w", encoding="utf-8").write(str(os.getpid()))
    time.sleep(30)
    os._exit(0)
open(parent_pid_file, "w", encoding="utf-8").write(str(os.getpid()))
time.sleep(30)
"""
        ran = run_bounded(
            [sys.executable, "-c", script, str(self.parent_pid_file), str(self.child_pid_file)],
            cwd=self.tmp_path,
            env={"PATH": os.environ.get("PATH", "/usr/bin"), "LANG": "C.UTF-8"},
            timeout_seconds=30,
        )
        return WorkerResult(
            ok=not ran.timed_out,
            summary="setsid sleeper finished",
            outputs={"timed_out": ran.timed_out, "marker": "setsid-result"},
        )


def test_execute_claim_timeout_kills_setsid_grandchild(tmp_path: Path) -> None:
    """A grandchild that starts a new session must still be reaped on kernel timeout."""
    worker = _SetsidGrandchildWorker(tmp_path)
    claim = _claim(task_id="task-setsid-cleanup", attempt_id="attempt-setsid", timeout_seconds=1)
    result, failure = execute_claim(
        claim,
        session_factory=_session_factory,
        worker_resolver=lambda _kind: worker,
    )
    assert failure == FailureClass.TIMEOUT
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (
        _child_alive(worker.parent_pid_file) or _child_alive(worker.child_pid_file)
    ):
        time.sleep(0.05)
    assert not _child_alive(worker.parent_pid_file), "session parent must be killed"
    assert not _child_alive(worker.child_pid_file), "setsid grandchild must be killed"
    assert result.outputs.get("kernel_timeout") is True or result.outputs.get("timed_out") is True


def test_claude_argv_can_disallow_subagent_tools() -> None:
    from max_agent.workers.cli_adapter import build_exec_argv

    argv = build_exec_argv(
        "claude",
        binary="claude",
        workspace=Path("/tmp"),
        prompt="write a short plan",
        prompt_file=Path("/tmp/prompt.txt"),
        tools=["Read", "Write", "Glob", "Grep", "Edit"],
        disallowed_tools=["Agent", "Task"],
    )
    assert "--disallowedTools" in argv
    assert "Agent,Task" in argv
    assert "--tools" in argv
    assert "Read,Write,Glob,Grep,Edit" in argv
    assert "-p" in argv
    assert "--dangerously-skip-permissions" not in argv
