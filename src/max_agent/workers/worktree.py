"""Per-task git worktree copied away from the source checkout.

The source repository is not registered as a worktree and is not edited.
`.git`, `.env`, and key material are not copied.
"""

from __future__ import annotations

import os
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
    # Preserve relative symlinks. The default (follow) raises FileNotFoundError
    # on dangling or host-absolute links, e.g. model-cache paths from a deploy host.
    # Absolute/external links are then replaced with a placeholder file so a
    # later CLI cannot resolve or follow the host target.
    shutil.copytree(seed_source, seed, ignore=_ignore, symlinks=True)
    _neutralize_external_symlinks(seed)
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


def _neutralize_external_symlinks(root: Path) -> None:
    """Replace absolute or escaping symlinks with a text placeholder. Do not follow targets."""
    root_res = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames) + list(filenames):
            path = Path(dirpath) / name
            if not path.is_symlink():
                continue
            target = os.readlink(path)
            external = os.path.isabs(target)
            if not external:
                dest = path.parent / target
                try:
                    dest.resolve().relative_to(root_res)
                except (ValueError, OSError):
                    external = True
            if not external:
                continue
            path.unlink()
            path.write_text(
                "# max-agent neutralized absolute/external symlink; target was not accessed\n"
                f"target={target}\n",
                encoding="utf-8",
            )


def _ignore(_directory: str, names: list[str]) -> set[str]:
    skipped: set[str] = set()
    for name in names:
        if (
            name in _SKIP_NAMES
            or name.startswith(".env")
            or name.endswith((".pem", ".key"))
            or name == "nohup.out"
        ):
            skipped.add(name)
    return skipped


def _git(cwd: Path, env: dict[str, str], args: list[str]) -> None:
    result = run_bounded(["git", *args], cwd=cwd, env=env, timeout_seconds=30, max_output_bytes=16_000)
    if result.timed_out or result.cancelled or result.exit_code != 0:
        detail = (result.stderr or result.stdout or "git failed").strip()
        raise WorktreeError(detail[:500])
