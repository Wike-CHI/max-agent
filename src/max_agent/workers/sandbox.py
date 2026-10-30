"""Bounded argv execution: no shell, capped output, redacted logs, killable process groups.

This is not a user-id, network, or container sandbox. It only constrains how this
process starts a child.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT_BYTES = 65_536
HARD_MAX_OUTPUT_BYTES = 262_144
_HARD_READ_BYTES = 1_048_576

_SECRET_PATTERNS = (
    re.compile(r"ghp_[A-Za-z0-9]{8,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
    re.compile(r"xai-[A-Za-z0-9]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(authorization|api[_-]?key|token|secret|password)\b\s*[:=]\s*\S+"),
)

_SENSITIVE_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "API_KEY",
    "APIKEY",
    "PRIVATE_KEY",
    "DATABASE",
    "WEBHOOK",
)
_SENSITIVE_PREFIXES = ("AWS_", "GITHUB_", "OPENAI_", "ANTHROPIC_", "XAI_", "GROK_")
ALLOWED_PASSTHROUGH = frozenset({"XAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"})


@dataclass
class BoundedRun:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    cancelled: bool
    truncated: bool
    argv: list[str]


def path_is_allowed(path: Path, roots: Sequence[Path]) -> bool:
    """True when the resolved path is the root or a path inside it."""
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            continue
        return True
    return False


def sensitive_key(name: str) -> bool:
    upper = name.upper()
    if any(marker in upper for marker in _SENSITIVE_MARKERS):
        return True
    return any(upper.startswith(prefix) for prefix in _SENSITIVE_PREFIXES)


def secret_values(environ: Mapping[str, str]) -> list[str]:
    values: list[str] = []
    for key, value in environ.items():
        if sensitive_key(key) and len(value) >= 8:
            values.append(value)
    return values


def redact(text: str, secrets: Sequence[str] | None = None) -> str:
    cleaned = text
    for secret in secrets or ():
        if secret and len(secret) >= 8:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[REDACTED]", cleaned)
    return cleaned


def child_env(
    *,
    home: Path,
    tmp: Path,
    extra_path_dirs: Sequence[str] = (),
    passthrough: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Allowlisted environment. Host credentials stay in the parent process."""
    parts = [str(path) for path in extra_path_dirs if path]
    for system in ("/usr/local/bin", "/usr/bin", "/bin"):
        if system not in parts:
            parts.append(system)
    env = {
        "PATH": os.pathsep.join(parts),
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "max-agent",
        "GIT_AUTHOR_EMAIL": "max-agent@localhost",
        "GIT_COMMITTER_NAME": "max-agent",
        "GIT_COMMITTER_EMAIL": "max-agent@localhost",
    }
    for key, value in (passthrough or {}).items():
        if key in ALLOWED_PASSTHROUGH and value:
            env[key] = value
    return env


def clamp_output_bytes(value: int) -> int:
    return max(32, min(int(value), HARD_MAX_OUTPUT_BYTES))


def run_bounded(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    cancel_file: Path | None = None,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
    secrets: Sequence[str] = (),
) -> BoundedRun:
    """Run `argv` with `shell=False`. A string command is rejected."""
    if isinstance(argv, str) or not isinstance(argv, (list, tuple)) or not argv:
        raise TypeError("argv must be a non-empty list of strings")
    if any(not isinstance(arg, str) for arg in argv):
        raise TypeError("argv must be a non-empty list of strings")
    argv_list = list(argv)
    limit = clamp_output_bytes(max_output_bytes)
    proc = subprocess.Popen(
        argv_list,
        cwd=str(cwd),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    stdout_count = [0]
    stderr_count = [0]
    threads = [
        threading.Thread(target=_drain, args=(proc.stdout, stdout_parts, stdout_count), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, stderr_parts, stderr_count), daemon=True),
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + max(timeout_seconds, 0.05)
    timed_out = False
    cancelled = False
    while proc.poll() is None:
        if cancel_file is not None and cancel_file.exists():
            cancelled = True
            _terminate(proc)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _terminate(proc)
            break
        time.sleep(0.05)
    if proc.poll() is None:
        _terminate(proc)
    for thread in threads:
        thread.join(timeout=2)

    raw_out = b"".join(stdout_parts)
    raw_err = b"".join(stderr_parts)
    stdout, out_trunc = _present(raw_out, limit, secrets)
    stderr, err_trunc = _present(raw_err, limit, secrets)
    exit_code = None if timed_out or cancelled else proc.returncode
    return BoundedRun(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        cancelled=cancelled,
        truncated=out_trunc or err_trunc or stdout_count[0] >= _HARD_READ_BYTES or stderr_count[0] >= _HARD_READ_BYTES,
        argv=argv_list,
    )


def _present(raw: bytes, limit: int, secrets: Sequence[str]) -> tuple[str, bool]:
    captured = raw[:_HARD_READ_BYTES]
    text = redact(captured.decode("utf-8", errors="replace"), secrets)
    truncated = len(raw) > limit or len(captured) < len(raw)
    if len(text.encode("utf-8")) > limit:
        text = text.encode("utf-8")[:limit].decode("utf-8", errors="ignore") + "\n[truncated]"
        truncated = True
    elif truncated:
        text += "\n[truncated]"
    return text, truncated


def _drain(pipe, parts: list[bytes], counter: list[int]) -> None:
    try:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                break
            if counter[0] < _HARD_READ_BYTES:
                room = _HARD_READ_BYTES - counter[0]
                parts.append(chunk[:room])
                counter[0] += len(chunk[:room])
    finally:
        pipe.close()


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        return
