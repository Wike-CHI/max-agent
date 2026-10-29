"""Fail if uv.lock pulls a non-PyPI source or an artifact without sha256."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from max_agent.ops.audit import audit_lock_text


def main() -> int:
    lock = ROOT / "uv.lock"
    errors = audit_lock_text(lock.read_text())
    if errors:
        print("dependency audit failed:", file=sys.stderr)
        for item in errors:
            print(item, file=sys.stderr)
        return 1
    print("dependency audit: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
