"""Run the live Postgres restore drill and archive a credential-free result."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from max_agent.ops.backup import run_live_postgres_drill
from max_agent.ops.provenance import current_revision


def main(argv: list[str] | None = None) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "operations" / "evidence" / f"postgres-restore-{today}.json",
    )
    args = parser.parse_args(argv)
    url = os.environ.get("MAX_AGENT_PG_DRILL", "").strip()
    evidence: dict[str, object] = {
        "format": "max-agent-postgres-restore-evidence-v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source_revision": current_revision(),
        "command": "uv run --no-sync --extra postgres python scripts/postgres_restore_drill.py",
    }
    exit_code = 0
    if not url:
        evidence.update(
            status="skipped",
            reason="MAX_AGENT_PG_DRILL is not set; no live PostgreSQL drill was run.",
        )
    else:
        try:
            with tempfile.TemporaryDirectory(prefix="max-pg-drill-") as workdir:
                report = run_live_postgres_drill(url, Path(workdir))
            evidence.update(status="passed" if report["ok"] else "failed", report=report)
            exit_code = 0 if report["ok"] else 1
        except Exception as exc:
            evidence.update(status="failed", error_type=type(exc).__name__)
            exit_code = 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
