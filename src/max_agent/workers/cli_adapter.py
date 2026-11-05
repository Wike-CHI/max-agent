"""CLI worker adapter.

Contract version 1. Inputs come from WorkerContext (goal, acceptance, timeout)
and worker_payload:

- vendor: auto | stub | grok | codex | claude
- model_tier: cheap | strong | auto. Omitted keeps today's vendor order.
- workspace_root: directory under /tmp (or MAX_AGENT_WORKSPACE_ROOT)
- repo_path: optional tree to copy; the source checkout is not edited
- cancel_file: touched by the caller to SIGTERM the child
- subprocess_timeout_seconds, max_output_bytes
- stub_sleep_seconds, stub_exit_code, stub_stdout_bytes, stub_canary
- require_real_worker: refuse stub fallback unless a real CLI is invoked

Outputs (also archived as `.max-agent/manifest.json`) include adapter_version,
vendor, model, provider, exit_code, stdout, stderr, timed_out, cancelled,
adapter_failure, artifact inventory, argv, require_real_worker, and
real_cli_invoked. A real invocation also copies a CLI usage report when one
is present. Real Grok Build / Codex / Claude Code processes start only when
`MAX_AGENT_ALLOW_EXTERNAL_CLI=1` and `grok version` / `codex --version` /
`claude --version` exits 0. Otherwise the run uses the local stub or returns a
classified skip. Authorized `auto` prefers Grok, then Codex, then Claude.
Model and provider are recorded for the process that actually started. Claude
uses a model in CLI JSON, then `ANTHROPIC_MODEL`, then
`CLAUDE_CODE_SUBAGENT_MODEL`. Other vendors use a model in CLI JSON when
present. Missing names are the string `unknown`, never `0`. A `measured` or
`estimate` usage report, or a vendor cost field such as `total_cost_usd`, is
attached for the attempt row. No report leaves cost unknown.
`model_tier=cheap` and `model_tier=auto` (with `vendor=auto`) prefer Claude,
then Grok. Codex is not on that path. `model_tier=strong` prefers Codex, then
Grok, then Claude, and is opt-in. An explicit vendor other than `auto` is
unchanged. The selected CLI still takes its model and base URL from the
allowlisted environment (the cc-switch cooperation point); argv does not gain
`--model` or a provider URL. An invalid tier fails with `invalid_model_tier`
before a stub run. Leaving `MAX_AGENT_ALLOW_EXTERNAL_CLI` unset still uses the
stub or the `require_real_worker` gate. A pytest failure does not escalate.
Setting `require_real_worker` in worker_payload or
`MAX_AGENT_REQUIRE_REAL_WORKER=1` makes stub selection and any result without
`real_cli_invoked=true` fail with `real_worker_required`.

Grok is invoked as headless `-p` with `--sandbox strict` (no `--yolo`).
Codex is invoked as `exec --sandbox workspace-write` (no `danger-full-access`).
Claude is invoked as headless `-p` with `--permission-mode acceptEdits`.
No flag set is a VM boundary. See docs/limitations.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from max_agent.models.enums import WorkerKind
from max_agent.models.schemas import UsageReport, WorkerResult
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
REQUIRE_REAL_ENV = "MAX_AGENT_REQUIRE_REAL_WORKER"
STUB_PATH = Path(__file__).resolve().parent / "cli_stub.py"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "cli_issue"

FORBIDDEN_CLI_FLAGS = frozenset(
    {
        "--yolo",
        "--always-approve",
        "--allow-dangerously-skip-permissions",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-skip-permissions",
        "danger-full-access",
    }
)

_NON_RECOVERABLE = frozenset(
    {
        "cli_not_authorized",
        "cli_unavailable",
        "invalid_model_tier",
        "path_violation",
        "real_worker_required",
        "worktree_failed",
    }
)
_VENDOR_ORDER = ("grok", "codex", "claude")
# cheap/auto are the default bulk path and must not start on Codex.
# strong is the only tier that prefers Codex. auto matches cheap until a
# later change adds a bounded escalate; this module does not escalate.
_TIER_VENDOR_ORDER = {
    "cheap": ("claude", "grok"),
    "auto": ("claude", "grok"),
    "strong": ("codex", "grok", "claude"),
}
_VENDOR_BINARY = {"grok": "grok", "codex": "codex", "claude": "claude"}
_VENDOR_PROVIDER = {"grok": "xai", "codex": "openai", "claude": "anthropic"}
_COST_LEDGERS = frozenset({"grok_bot_quota", "cli_quota", "api_spend"})
_USAGE_MARKERS = frozenset({"measured", "estimate"})
_USD_COST_KEYS = ("total_cost_usd", "cost_usd", "costUSD")
_VENDOR_VERSION = {"grok": ("version",), "codex": ("--version",), "claude": ("--version",)}
_VENDOR_PASSTHROUGH = {
    "grok": ("XAI_API_KEY",),
    "codex": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "claude": (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
        "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
        "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ),
}


def require_real_worker(payload: dict | None = None, environ: Mapping | None = None) -> bool:
    env = os.environ if environ is None else environ
    value = (payload or {}).get("require_real_worker")
    return env.get(REQUIRE_REAL_ENV) == "1" or value is True or (
        isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"}
    )


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


def resolve_cli_identity(
    vendor: str,
    stdout: str,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str, dict[str, Any] | None]:
    """Return model, provider, and an optional usage report.

    Model comes from CLI JSON, then from ``ANTHROPIC_MODEL`` /
    ``CLAUDE_CODE_SUBAGENT_MODEL`` for Claude. Provider comes from JSON, then
    from the vendor (``xai``, ``openai``, ``anthropic``). A name that is still
    missing is the string ``unknown``. A cost report is returned only for an
    explicit measured/estimate amount or a vendor cost field. Other numbers
    are dropped instead of being stored as ``0``.
    """
    env = {} if environ is None else environ
    payload = _parse_cli_json(stdout)
    usage_raw = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    model = _model_from_payload(payload) or _host_model(vendor, env) or "unknown"
    provider = _provider_name(vendor, payload, usage_raw)
    usage = _usage_report(payload, model=model, provider=provider)
    return model, provider, usage


class InvalidModelTier(ValueError):
    """`model_tier` was set to something other than cheap, strong, or auto."""

    def __init__(self, token: str) -> None:
        self.token = token
        super().__init__(token)


@dataclass(frozen=True)
class Route:
    vendor: str
    failure: str | None
    model_tier: str | None
    vendor_order: tuple[str, ...]


def parse_model_tier(value: Any) -> str | None:
    """Return a normalized tier, or None when the field is omitted.

    Empty strings count as omitted. Any other value raises InvalidModelTier.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return None
        if text in _TIER_VENDOR_ORDER:
            return text
        raise InvalidModelTier(text[:64])
    raise InvalidModelTier(type(value).__name__[:64])


def resolve_route(
    requested: str,
    model_tier: Any,
    probes: list[Probe],
    authorized: bool,
) -> Route:
    """Select a vendor for `model_tier` without bypassing the CLI gate.

    Omitted tier keeps `choose_vendor` (authorized `auto` is Grok, Codex, then
    Claude). `cheap` and `auto` use Claude, then Grok, and never Codex.
    `strong` uses Codex, then Grok, then Claude. An explicit vendor other than
    `auto` is not reordered. An unknown tier returns `invalid_model_tier`
    instead of a stub success. Authorization is still `choose_vendor`'s:
    a closed external-CLI gate selects stub for `auto`, or `cli_not_authorized`
    for an explicit real vendor.
    """
    try:
        tier = parse_model_tier(model_tier)
    except InvalidModelTier as exc:
        return Route(vendor="stub", failure="invalid_model_tier", model_tier=exc.token, vendor_order=())
    if requested == "auto" and tier is not None:
        order = _TIER_VENDOR_ORDER[tier]
        vendor, failure = choose_vendor(requested, probes, authorized, order=order)
        return Route(vendor=vendor, failure=failure, model_tier=tier, vendor_order=order)
    order = _VENDOR_ORDER if requested == "auto" else (requested,)
    vendor, failure = choose_vendor(requested, probes, authorized)
    return Route(vendor=vendor, failure=failure, model_tier=tier, vendor_order=order)


def choose_vendor(
    requested: str,
    probes: list[Probe],
    authorized: bool,
    order: tuple[str, ...] | None = None,
) -> tuple[str, str | None]:
    """Pick a vendor. Authorized `auto` prefers Grok, Codex, then Claude.

    `order` replaces that preference for a model tier. It does not authorize
    a real CLI and does not read `model_tier` itself.
    """
    names = _VENDOR_ORDER if order is None else order
    by_name = {probe.vendor: probe for probe in probes}
    if requested == "auto":
        if authorized:
            for name in names:
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
    elif vendor == "claude":
        argv = [
            binary,
            "--permission-mode",
            "acceptEdits",
            "--output-format",
            "json",
            "-p",
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
    return [_probe_one(name, authorized=authorized) for name in _VENDOR_ORDER]


class CliWorker(Worker):
    kind = WorkerKind.CLI

    def capabilities(self) -> set[str]:
        return {"cli", "pytest", "worktree", "stub"}

    def run(self, ctx: WorkerContext) -> WorkerResult:
        payload = dict(ctx.payload)
        requested = str(payload.get("vendor") or "auto")
        required = require_real_worker(payload)
        authorized = os.environ.get(ALLOW_ENV) == "1"
        preview = resolve_route(requested, payload.get("model_tier"), [], authorized)
        if preview.failure == "invalid_model_tier":
            probes = []
            route = preview
        else:
            probes = probe_vendors()
            route = resolve_route(requested, payload.get("model_tier"), probes, authorized)
        vendor = route.vendor
        failure = route.failure
        secrets = secret_values(os.environ)
        tool_calls: list[dict[str, Any]] = []

        def note(tool: str, args: dict[str, Any]) -> None:
            safe_args = {key: redact(str(value), secrets) for key, value in args.items()}
            tool_calls.append({"tool": tool, "args": safe_args})
            if ctx.on_tool_call:
                ctx.on_tool_call(tool, safe_args)

        note(
            "cli.probe",
            {
                "vendor": vendor,
                "authorized": authorized,
                "failure": failure or "",
                "model_tier": route.model_tier or "",
            },
        )
        if failure is None and required and vendor == "stub":
            failure = "real_worker_required"
        if failure:
            if failure == "invalid_model_tier":
                summary = f"invalid model_tier {route.model_tier!r}; expected cheap, strong, or auto"
            elif failure == "real_worker_required":
                summary = "stub refused because a real CLI worker is required"
            else:
                summary = failure
            return self._finish(
                ok=False,
                summary=summary,
                failure=failure,
                vendor=vendor,
                authorized=authorized,
                required=required,
                probes=probes,
                tool_calls=tool_calls,
                secrets=secrets,
                error=summary,
                model_tier=route.model_tier,
                route_vendors=route.vendor_order,
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
                required=required,
                probes=probes,
                tool_calls=tool_calls,
                secrets=secrets,
                error=str(exc),
                model_tier=route.model_tier,
                route_vendors=route.vendor_order,
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
                required=required,
                probes=probes,
                tool_calls=tool_calls,
                secrets=secrets,
                error=redact(str(exc), secrets),
                model_tier=route.model_tier,
                route_vendors=route.vendor_order,
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
        if required and not real:
            failure = "real_worker_required"
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
            required=required,
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
            model_tier=route.model_tier,
            route_vendors=route.vendor_order,
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
            usage=_result_usage(report),
        )

    def _finish(
        self,
        *,
        ok: bool,
        summary: str,
        failure: str | None,
        vendor: str,
        authorized: bool,
        required: bool,
        probes: list[Probe],
        tool_calls: list[dict[str, Any]],
        secrets: list[str],
        error: str | None,
        model_tier: str | None,
        route_vendors: tuple[str, ...],
    ) -> WorkerResult:
        report = _base_report(
            vendor=vendor,
            authorized=authorized,
            required=required,
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
            model_tier=model_tier,
            route_vendors=route_vendors,
        )
        report["stdout"] = redact(report["stdout"], secrets)
        return WorkerResult(
            ok=ok,
            summary=summary,
            outputs=report,
            tool_calls=tool_calls,
            recoverable=failure not in _NON_RECOVERABLE if failure else True,
            error=redact(error or summary, secrets),
            usage=_result_usage(report),
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


def _label(value: Any, *, limit: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text == "0":
        return None
    return text[:limit]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number < 0 or number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _whole(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _parse_cli_json(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        return {}
    parsed = _json_object(text)
    if parsed is not None:
        return parsed
    for line in reversed(text.splitlines()):
        parsed = _json_object(line.strip())
        if parsed is not None:
            return parsed
    return {}


def _json_object(text: str) -> dict[str, Any] | None:
    if not text or text[0] not in "{[":
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict):
        return value
    return None


def _host_model(vendor: str, env: Mapping[str, str]) -> str | None:
    if vendor != "claude":
        return None
    return _label(env.get("ANTHROPIC_MODEL")) or _label(env.get("CLAUDE_CODE_SUBAGENT_MODEL"))


def _model_from_payload(payload: dict[str, Any]) -> str | None:
    for key in ("model", "model_name", "modelName"):
        found = _label(payload.get(key))
        if found:
            return found
    usage = payload.get("usage")
    if isinstance(usage, dict):
        found = _label(usage.get("model"))
        if found:
            return found
    model_usage = payload.get("modelUsage")
    if isinstance(model_usage, dict):
        names = [_label(key) for key in model_usage]
        names = [name for name in names if name]
        if len(names) == 1:
            return names[0]
    return None


def _provider_name(vendor: str, payload: dict[str, Any], usage: dict[str, Any]) -> str:
    found = _label(payload.get("provider")) or _label(usage.get("provider"))
    if found:
        return found
    return _VENDOR_PROVIDER.get(vendor, "unknown")


def _ledger(value: Any) -> str | None:
    text = _label(value, limit=64)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in _COST_LEDGERS:
        return lowered
    return None


def _marker(value: Any) -> str | None:
    text = _label(value, limit=32)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in _USAGE_MARKERS:
        return lowered
    return None


def _first_usd(data: Mapping[str, Any]) -> float | None:
    for key in _USD_COST_KEYS:
        amount = _number(data.get(key))
        if amount is not None:
            return amount
    return None


def _vendor_cost(payload: dict[str, Any], usage: dict[str, Any]) -> tuple[float, str | None] | None:
    amount = _first_usd(payload)
    if amount is not None:
        return amount, "USD"
    amount = _first_usd(usage)
    if amount is not None:
        return amount, "USD"
    model_usage = payload.get("modelUsage")
    if isinstance(model_usage, dict) and len(model_usage) == 1:
        entry = next(iter(model_usage.values()))
        if isinstance(entry, dict):
            nested = _first_usd(entry)
            if nested is not None:
                return nested, "USD"
    amount = _number(usage.get("cost"))
    if amount is not None:
        return amount, _label(usage.get("currency"), limit=16)
    return None


def _token_pair(payload: dict[str, Any], usage: dict[str, Any]) -> tuple[int | None, int | None]:
    input_tokens = _whole(usage.get("input_tokens"))
    if input_tokens is None:
        input_tokens = _whole(usage.get("prompt_tokens"))
    output_tokens = _whole(usage.get("output_tokens"))
    if output_tokens is None:
        output_tokens = _whole(usage.get("completion_tokens"))
    model_usage = payload.get("modelUsage")
    if isinstance(model_usage, dict) and len(model_usage) == 1:
        entry = next(iter(model_usage.values()))
        if isinstance(entry, dict):
            if input_tokens is None:
                input_tokens = _whole(entry.get("inputTokens"))
            if output_tokens is None:
                output_tokens = _whole(entry.get("outputTokens"))
    return input_tokens, output_tokens


def _usage_report(
    payload: dict[str, Any],
    *,
    model: str,
    provider: str,
) -> dict[str, Any] | None:
    if not payload:
        return None
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    marker = _marker(usage.get("marker"))
    amount = _number(usage.get("amount"))
    currency = _label(usage.get("currency"), limit=16)
    source = _ledger(usage.get("source"))
    if marker is None or amount is None:
        found = _vendor_cost(payload, usage)
        if found is None:
            return None
        amount, vendor_currency = found
        marker = marker or "measured"
        currency = vendor_currency or currency
    if amount is None or marker is None:
        return None
    if source is None:
        source = "api_spend" if currency == "USD" else "cli_quota"
    report: dict[str, Any] = {"source": source, "marker": marker, "amount": amount}
    if currency:
        report["currency"] = currency
    input_tokens, output_tokens = _token_pair(payload, usage)
    if input_tokens is not None:
        report["input_tokens"] = input_tokens
    if output_tokens is not None:
        report["output_tokens"] = output_tokens
    if model != "unknown":
        report["model"] = model
    if provider != "unknown":
        report["provider"] = provider
    return report


def _result_usage(report: dict[str, Any]) -> UsageReport | None:
    raw = report.get("usage")
    if not isinstance(raw, dict):
        return None
    return UsageReport.model_validate(raw)


def _base_report(
    *,
    vendor: str,
    authorized: bool,
    required: bool,
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
    model_tier: str | None = None,
    route_vendors: tuple[str, ...] = (),
) -> dict[str, Any]:
    model, provider, usage = resolve_cli_identity(
        vendor,
        stdout if real else "",
        os.environ if real else {},
    )
    report = {
        "adapter_version": ADAPTER_VERSION,
        "vendor": vendor,
        "model": model,
        "provider": provider,
        "model_tier": model_tier,
        "route_vendors": list(route_vendors),
        "cli_invoked": invoked,
        "real_cli_invoked": real and invoked,
        "external_cli_authorized": authorized,
        "require_real_worker": required,
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
    if usage is not None:
        report["usage"] = usage
    return report


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
        "model": report.get("model"),
        "provider": report.get("provider"),
        "model_tier": report.get("model_tier"),
        "route_vendors": list(report.get("route_vendors") or []),
        "cli_invoked": report["cli_invoked"],
        "real_cli_invoked": report["real_cli_invoked"],
        "require_real_worker": report["require_real_worker"],
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
