"""Staging smoke against an in-process app and a temporary SQLite file."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from max_agent.ops.smoke import run_smoke


def main() -> int:
    database = Path(tempfile.mkdtemp(prefix="max-smoke-")) / "smoke.db"
    try:
        report = run_smoke(database_url=f"sqlite:///{database}")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
