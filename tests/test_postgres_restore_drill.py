"""Credential-free evidence for the opt-in live Postgres drill."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_evidence_runner_records_an_honest_skip(tmp_path: Path) -> None:
    output = tmp_path / "evidence.json"
    env = os.environ.copy()
    env.pop("MAX_AGENT_PG_DRILL", None)
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts" / "postgres_restore_drill.py"),
            "--output",
            str(output),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    evidence = json.loads(output.read_text())
    assert evidence["status"] == "skipped"
    assert "no live PostgreSQL drill was run" in evidence["reason"]
    assert "MAX_AGENT_PG_DRILL" not in evidence["command"]
