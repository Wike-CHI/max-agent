"""Per-task git worktree copied away from the source checkout.

The source repository is not registered as a worktree and is not edited.
`.git`, `.env`, and key material are not copied.
"""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from max_agent.workers.sandbox import child_env, run_bounded

_SKIP_NAMES = {".git", ".ssh", "__pycache__", ".max-agent", "id_rsa", "id_ed25519", "credentials.json"}


class WorktreeError(RuntimeError):
    pass


def create_isolated_worktree(*, task_id: str, workspace_root: Path, seed_source: Path) -> Path:
    if not seed_source.is_dir():
        raise WorktreeError(f"seed is not a directory: {seed_source}")
    safe_task = re.sub(r"[^A-Za-z0-9_-]", "_", task_id)[:80] or "task"
    run_root = workspace_root / safe_task / uuid.uuid4().hex[:12]
    seed = run_root / "seed"
    work = run_root / "work"
    home = run_root / ".home"
    tmp = run_root / ".tmp"
    run_root.mkdir(parents=True, exist_ok=False)
    home.mkdir()
    tmp.mkdir()
    shutil.copytree(seed_source, seed, ignore=_ignore)
    env = child_env(home=home, tmp=tmp)
    _git(seed, env, ["init", "-b", "main"])
    _git(seed, env, ["add", "-A"])
    _git(
        seed,
        env,
        ["-c", "user.email=max-agent@localhost", "-c", "user.name=max-agent", "commit", "-m", "seed isolated workspace"],
    )
    _git(seed, env, ["worktree", "add", "--detach", str(work), "HEAD"])
    return work


def _ignore(_directory: str, names: list[str]) -> set[str]:
    skipped: set[str] = set()
    for name in names:
        if name in _SKIP_NAMES or name.startswith(".env") or name.endswith((".pem", ".key")):
            skipped.add(name)
    return skipped


def _git(cwd: Path, env: dict[str, str], args: list[str]) -> None:
    result = run_bounded(["git", *args], cwd=cwd, env=env, timeout_seconds=30, max_output_bytes=16_000)
    if result.timed_out or result.cancelled or result.exit_code != 0:
        detail = (result.stderr or result.stdout or "git failed").strip()
        raise WorktreeError(detail[:500])
