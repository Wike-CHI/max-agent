"""Deterministic stand-in for a coding CLI.

Applies the fixed fixture patch in an already isolated workspace. It does not
call a network or read the parent environment on purpose; the parent passes an
allowlisted env.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="max-agent-cli-stub")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--sleep-seconds", type=float, default=0)
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--stdout-bytes", type=int, default=0)
    parser.add_argument("--canary", default="")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace)
    meta = workspace / ".max-agent"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "child-env-keys.json").write_text(json.dumps(sorted(os_keys())), encoding="utf-8")
    if args.sleep_seconds > 0:
        time.sleep(args.sleep_seconds)
    if args.canary:
        print(args.canary, flush=True)
    if args.stdout_bytes > 0:
        sys.stdout.buffer.write(b"x" * args.stdout_bytes)
        sys.stdout.buffer.flush()
    if args.exit_code != 0:
        return args.exit_code
    _apply_fixture(workspace)
    return 0


def os_keys() -> list[str]:
    import os

    return list(os.environ)


def _apply_fixture(workspace: Path) -> None:
    calc = workspace / "calc.py"
    text = calc.read_text(encoding="utf-8") if calc.is_file() else ""
    if "def add" not in text or "NotImplementedError" not in text:
        raise SystemExit(3)
    calc.write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
