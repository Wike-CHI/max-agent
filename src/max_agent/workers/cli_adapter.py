"""CLI worker adapter.

Contract version 1. Inputs come from WorkerContext (goal, acceptance, timeout)
and worker_payload:

- vendor: auto | stub | grok | codex
- workspace_root: directory under /tmp (or MAX_AGENT_WORKSPACE_ROOT)
- repo_path: optional tree to copy; the source checkout is not edited
- cancel_file: touched by the caller to SIGTERM the child
- subprocess_timeout_seconds, max_output_bytes
- stub_sleep_seconds, stub_exit_code, stub_stdout_bytes, stub_canary

Outputs (also archived as `.max-agent/manifest.json`) include adapter_version,
vendor, exit_code, stdout, stderr, timed_out, cancelled, adapter_failure,
artifact inventory, and argv. Real Grok Build / Codex processes start only when
`MAX_AGENT_ALLOW_EXTERNAL_CLI=1` and `grok version` / `codex --version` exits 0.
Otherwise the run uses the local stub or returns a classified skip.

Grok is invoked as headless `-p` with `--sandbox strict` (no `--yolo`).
Codex is invoked as `exec --sandbox workspace-write` (no `danger-full-access`).
Neither flag set is a VM boundary. See docs/limitations.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from max_agent.models.enums import WorkerKind
from max_agent.models.schemas import WorkerResult
from max_agent.workers.base import Worker, WorkerContext
from max_agent.workers.sandbox import (
    child_env,
    clamp_output_bytes,
    path_is_allowed,
    redact,
    run_bounded,
    secret_values,
)
from max_agent.workers.worktree import WorktreeError, create_isolated_worktree

ADAPTER_VERSION = "1"
ALLOW_ENV = "MAX_AGENT_ALLOW_EXTERNAL_CLI"
STUB_PATH = Path(__file__).resolve().parent / "cli_stub.py"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "cli_issue"

FORBIDDEN_CLI_FLAGS = frozenset(
    {
        "--yolo",
        "--always-approve",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-skip-permissions",
        "danger-full-access",
    }
)

_NON_RECOVERABLE = frozenset(
    {"cli_not_authorized", "cli_unavailable", "path_violation", "worktree_failed"}
)
_VENDOR_BINARY = {"grok": "grok", "codex": "codex"}
_VENDOR_VERSION = {"grok": ("version",), "codex": ("--version",)}
_VENDOR_PASSTHROUGH = {"grok": ("XAI_API_KEY",), "codex": ("OPENAI_API_KEY",)}


@dataclass(frozen=True)
class Probe:
    vendor: str
    available: bool
    enabled: bool
    reason: str
    binary: str | None = None
    version_exit_code: int | None = None


def cli_workspace_roots() -> tuple[Path, ...]:
    roots = [Path(tempfile.gettempdir()), Path("/tmp")]
    extra = os.environ.get("MAX_AGENT_WORKSPACE_ROOT")
    if extra:
        roots.append(Path(extra))
    return tuple(roots)


def choose_vendor(requested: str, probes: list[Probe], authorized: bool) -> tuple[str, str | None]:
    """Pick a vendor. `auto` uses a probed real CLI only when authorized."""
    by_name = {probe.vendor: probe for probe in probes}
    if requested == "auto":
        if authorized:
            for name in ("grok", "codex"):
                probe = by_name.get(name)
                if probe is not None and probe.enabled:
                    return name, None
        return "stub", None
    if requested == "stub":
        return "stub", None
    if requested not in _VENDOR_BINARY:
        return requested, "cli_unavailable"
    if not authorized:
        return requested, "cli_not_authorized"
    probe = by_name.get(requested)
    if probe is None or not probe.enabled:
        return requested, "cli_unavailable"
    return requested, None


def build_exec_argv(
    vendor: str,
    *,
    binary: str,
    workspace: Path,
    prompt: str,
    prompt_file: Path,
) -> list[str]:
    """Argv for one headless coding CLI. The prompt is a single argument."""
    del prompt_file  # real CLIs receive the prompt as one argv element
    if vendor == "grok":
        argv = [
            binary,
            "--no-auto-update",
            "--cwd",
            str(workspace),
            "--sandbox",
            "strict",
            "--permission-mode",
            "acceptEdits",
            "--disable-web-search",
            "--no-memory",
            "--no-subagents",
            "--output-format",
            "json",
            "-p",
            prompt,
        ]
    elif vendor == "codex":
        argv = [
            binary,
            "exec",
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "never",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--color",
            "never",
            "-C",
            str(workspace),
            "--output-last-message",
            str(workspace / ".max-agent" / "cli-last-message.txt"),
            prompt,
        ]
    else:
        raise ValueError(f"no real CLI argv for vendor {vendor}")
    blocked = FORBIDDEN_CLI_FLAGS.intersection(argv)
    if blocked:
        raise RuntimeError(f"refusing forbidden CLI flags: {sorted(blocked)}")
    return argv


def probe_vendors(environ: dict[str, str] | None = None) -> list[Probe]:
    env = os.environ if environ is None else environ
    authorized = env.get(ALLOW_ENV) == "1"
    return [_probe_one(name, authorized=authorized) for name in ("grok", "codex")]


class CliWorker(Worker):
    kind = WorkerKind.CLI

    def capabilities(self) -> set[str]:
        return {"cli", "pytest", "worktree", "stub"}

    def run(self, ctx: WorkerContext) -> WorkerResult:
        payload = dict(ctx.payload)
        requested = str(payload.get("vendor") or "auto")
        authorized = os.environ.get(ALLOW_ENV) == "1"
        probes = probe_vendors()
        vendor, failure = choose_vendor(requested, probes, authorized)
        secrets = secret_values(os.environ)
        tool_calls: list[dict[str, Any]] = []

        def note(tool: str, args: dict[str, Any]) -> None:
            safe_args = {key: redact(str(value), secrets) for key, value in args.items()}
            tool_calls.append({"tool": tool, "args": safe_args})
            if ctx.on_tool_call:
                ctx.on_tool_call(tool, safe_args)

        note("cli.probe", {"vendor": vendor, "authorized": authorized, "failure": failure or ""})
        if failure:
            return self._finish(
                ok=False,
                summary=failure,
                failure=failure,
                vendor=vendor,
                authorized=authorized,
                probes=probes,
                tool_calls=tool_calls,
                secrets=secrets,
                error=failure,
            )

        try:
            workspace_root = _workspace_root(payload)
        except ValueError as exc:
            return self._finish(
                ok=False,
                summary="workspace path not allowed",
                failure="path_violation",
                vendor=vendor,
                authorized=authorized,
                probes=probes,
                tool_calls=tool_calls,
                secrets=secrets,
                error=str(exc),
            )

        try:
            seed = _seed_source(payload)
            workspace = create_isolated_worktree(
                task_id=ctx.task_id,
                workspace_root=workspace_root,
                seed_source=seed,
            )
        except (WorktreeError, OSError) as exc:
            return self._finish(
                ok=False,
                summary="isolated worktree failed",
                failure="worktree_failed",
                vendor=vendor,
                authorized=authorized,
                probes=probes,
                tool_calls=tool_calls,
                secrets=secrets,
                error=redact(str(exc), secrets),
            )

        meta = workspace / ".max-agent"
        home = workspace / ".home"
        tmp = workspace / ".tmp"
        for directory in (meta, home, tmp):
            directory.mkdir(parents=True, exist_ok=True)
        prompt = _render_prompt(ctx, workspace)
        prompt_file = meta / "prompt.txt"
        prompt_file.write_text(redact(prompt, secrets), encoding="utf-8")
        limit = clamp_output_bytes(int(payload.get("max_output_bytes") or 65_536))
        cli_timeout = _cli_timeout(ctx, payload)
        cancel = Path(str(payload["cancel_file"])) if payload.get("cancel_file") else None

        if vendor == "stub":
            argv = _stub_argv(workspace, prompt_file, payload)
            passthrough: dict[str, str] = {}
            real = False
        else:
            binary = next(probe.binary for probe in probes if probe.vendor == vendor and probe.binary)
            argv = build_exec_argv(
                vendor,
                binary=binary,
                workspace=workspace,
                prompt=redact(prompt, secrets),
                prompt_file=prompt_file,
            )
            passthrough = _passthrough(vendor)
            real = True

        env = child_env(
            home=home,
            tmp=tmp,
            extra_path_dirs=[str(Path(argv[0]).resolve().parent)] if vendor != "stub" else [],
            passthrough=passthrough,
        )
        note("cli.exec", {"vendor": vendor, "argc": str(len(argv))})
        ran = run_bounded(
            argv,
            cwd=workspace,
            env=env,
            timeout_seconds=cli_timeout,
            cancel_file=cancel,
            max_output_bytes=limit,
            secrets=secrets,
        )
        failure = _classify_run(ran)
        pytest_exit: int | None = None
        pytest_stdout = ""
        pytest_truncated = False
        if failure is None:
            pytest_ran = run_bounded(
                [sys.executable, "-m", "pytest", "-q", "--noconftest", "--rootdir", str(workspace), "test_calc.py"],
                cwd=workspace,
                env=child_env(home=home, tmp=tmp),
                timeout_seconds=max(min(ctx.timeout_seconds, 120), 1),
                max_output_bytes=limit,
                secrets=secrets,
            )
            note("cli.pytest", {"exit_code": pytest_ran.exit_code if pytest_ran.exit_code is not None else ""})
            pytest_exit = pytest_ran.exit_code
            pytest_stdout = pytest_ran.stdout
            pytest_truncated = pytest_ran.truncated
            if pytest_ran.timed_out:
                failure = "timeout"
            elif pytest_ran.cancelled:
                failure = "cancelled"
            elif pytest_ran.exit_code != 0:
                failure = "pytest_failed"

        report = _base_report(
            vendor=vendor,
            authorized=authorized,
            probes=probes,
            invoked=True,
            real=real,
            workspace=workspace,
            argv=_redact_argv(argv, secrets),
            exit_code=ran.exit_code,
            stdout=ran.stdout,
            stderr=ran.stderr,
            timed_out=ran.timed_out or failure == "timeout",
            cancelled=ran.cancelled or failure == "cancelled",
            truncated=ran.truncated or pytest_truncated,
            failure=failure,
            pytest_exit_code=pytest_exit,
        )
        report["pytest_stdout"] = pytest_stdout
        manifest = _archive(workspace, report)
        report["artifact_manifest"] = str(manifest)
        report["artifacts"] = _inventory(workspace)
        ok = failure is None
        summary = "cli fixture verified" if ok else (failure or "cli failed")
        return WorkerResult(
            ok=ok,
            summary=summary,
            outputs=report,
            tool_calls=tool_calls,
            recoverable=failure not in _NON_RECOVERABLE if failure else True,
            error=None if ok else summary,
        )

    def _finish(
        self,
        *,
        ok: bool,
        summary: str,
        failure: str | None,
        vendor: str,
        authorized: bool,
        probes: list[Probe],
        tool_calls: list[dict[str, Any]],
        secrets: list[str],
        error: str | None,
    ) -> WorkerResult:
        report = _base_report(
            vendor=vendor,
            authorized=authorized,
            probes=probes,
            invoked=False,
            real=False,
            workspace=None,
            argv=[],
            exit_code=None,
            stdout="",
            stderr="",
            timed_out=False,
            cancelled=False,
            truncated=False,
            failure=failure,
            pytest_exit_code=None,
        )
        report["stdout"] = redact(report["stdout"], secrets)
        return WorkerResult(
            ok=ok,
            summary=summary,
            outputs=report,
            tool_calls=tool_calls,
            recoverable=failure not in _NON_RECOVERABLE if failure else True,
            error=redact(error or summary, secrets),
        )


def _probe_one(vendor: str, *, authorized: bool) -> Probe:
    binary_name = _VENDOR_BINARY[vendor]
    found = shutil.which(binary_name)
    if not authorized:
        return Probe(
            vendor=vendor,
            available=found is not None,
            enabled=False,
            reason="not_authorized" if found else "not_on_path",
            binary=found,
        )
    if found is None:
        return Probe(vendor=vendor, available=False, enabled=False, reason="not_on_path")
    home = Path(tempfile.mkdtemp(prefix="max-agent-probe-home-"))
    tmp = Path(tempfile.mkdtemp(prefix="max-agent-probe-tmp-"))
    try:
        ran = run_bounded(
            [found, *_VENDOR_VERSION[vendor]],
            cwd=Path(tempfile.gettempdir()),
            env=child_env(home=home, tmp=tmp, extra_path_dirs=[str(Path(found).resolve().parent)]),
            timeout_seconds=5,
            max_output_bytes=4_096,
            secrets=secret_values(os.environ),
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    enabled = ran.exit_code == 0 and not ran.timed_out and not ran.cancelled
    reason = "ok" if enabled else "version_probe_failed"
    return Probe(
        vendor=vendor,
        available=True,
        enabled=enabled,
        reason=reason,
        binary=found,
        version_exit_code=ran.exit_code,
    )


def _workspace_root(payload: dict[str, Any]) -> Path:
    raw = payload.get("workspace_root")
    root = Path(str(raw)).expanduser() if raw else Path(tempfile.gettempdir()) / "max-agent-workspaces"
    resolved = root.resolve()
    if not path_is_allowed(resolved, cli_workspace_roots()):
        raise ValueError(f"refusing workspace outside the allowlist: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _seed_source(payload: dict[str, Any]) -> Path:
    raw = payload.get("repo_path")
    if not raw:
        return FIXTURE_DIR
    path = Path(str(raw)).expanduser().resolve()
    if not path.is_dir():
        raise WorktreeError(f"repo_path is not a directory: {path}")
    if not path_is_allowed(path, cli_workspace_roots()):
        raise WorktreeError(f"refusing repo_path outside the allowlist: {path}")
    return path


def _render_prompt(ctx: WorkerContext, workspace: Path) -> str:
    issue = workspace / "ISSUE.md"
    issue_text = issue.read_text(encoding="utf-8")[:16_000] if issue.is_file() else ""
    criteria = "\n".join(f"- {item}" for item in ctx.acceptance_criteria)
    return (
        "You are a coding worker inside an isolated git worktree.\n"
        "Change only files in this workspace. Do not read credentials or call the network.\n"
        f"Goal:\n{ctx.goal}\n\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        f"Issue:\n{issue_text}\n"
    )


def _cli_timeout(ctx: WorkerContext, payload: dict[str, Any]) -> float:
    requested = payload.get("subprocess_timeout_seconds")
    budget = float(ctx.timeout_seconds)
    if requested is None:
        return max(budget, 0.1)
    return max(min(float(requested), budget), 0.1)


def _stub_argv(workspace: Path, prompt_file: Path, payload: dict[str, Any]) -> list[str]:
    argv = [
        sys.executable,
        str(STUB_PATH),
        "--workspace",
        str(workspace),
        "--prompt-file",
        str(prompt_file),
    ]
    if payload.get("stub_sleep_seconds"):
        argv.extend(["--sleep-seconds", str(payload["stub_sleep_seconds"])])
    if payload.get("stub_exit_code"):
        argv.extend(["--exit-code", str(payload["stub_exit_code"])])
    if payload.get("stub_stdout_bytes"):
        argv.extend(["--stdout-bytes", str(payload["stub_stdout_bytes"])])
    if payload.get("stub_canary"):
        argv.extend(["--canary", str(payload["stub_canary"])])
    return argv


def _passthrough(vendor: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in _VENDOR_PASSTHROUGH.get(vendor, ()):
        raw = os.environ.get(key)
        if raw:
            values[key] = raw
    return values


def _classify_run(ran) -> str | None:
    if ran.timed_out:
        return "timeout"
    if ran.cancelled:
        return "cancelled"
    if ran.exit_code != 0:
        return "nonzero_exit"
    return None


def _redact_argv(argv: list[str], secrets: list[str]) -> list[str]:
    return [redact(arg, secrets) for arg in argv]


def _base_report(
    *,
    vendor: str,
    authorized: bool,
    probes: list[Probe],
    invoked: bool,
    real: bool,
    workspace: Path | None,
    argv: list[str],
    exit_code: int | None,
    stdout: str,
    stderr: str,
    timed_out: bool,
    cancelled: bool,
    truncated: bool,
    failure: str | None,
    pytest_exit_code: int | None,
) -> dict[str, Any]:
    return {
        "adapter_version": ADAPTER_VERSION,
        "vendor": vendor,
        "cli_invoked": invoked,
        "real_cli_invoked": real and invoked,
        "external_cli_authorized": authorized,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "adapter_failure": failure,
        "artifacts": [],
        "artifact_manifest": None,
        "workspace": str(workspace) if workspace else None,
        "pytest_exit_code": pytest_exit_code,
        "output_truncated": truncated,
        "probes": [asdict(probe) for probe in probes],
        "argv": argv,
    }


def _inventory(workspace: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(workspace)
        if ".git" in rel.parts or (rel.parts and rel.parts[0] == ".max-agent"):
            continue
        size = path.stat().st_size
        if size > 2_000_000:
            continue
        items.append(
            {
                "path": rel.as_posix(),
                "size": size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
        if len(items) >= 200:
            break
    return items


def _archive(workspace: Path, report: dict[str, Any]) -> Path:
    meta = workspace / ".max-agent"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "stdout.log").write_text(str(report.get("stdout") or ""), encoding="utf-8")
    (meta / "stderr.log").write_text(str(report.get("stderr") or ""), encoding="utf-8")
    manifest = meta / "manifest.json"
    body = {
        "adapter_version": report["adapter_version"],
        "vendor": report["vendor"],
        "exit_code": report["exit_code"],
        "adapter_failure": report["adapter_failure"],
        "timed_out": report["timed_out"],
        "cancelled": report["cancelled"],
        "pytest_exit_code": report["pytest_exit_code"],
        "argv": report["argv"],
        "artifacts": _inventory(workspace),
    }
    manifest.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return manifest
