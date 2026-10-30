"""Schema rollback and journal restore. Live Postgres is optional."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from max_agent.ops.backup import (
    build_pg_dump_command,
    format_command_for_log,
    run_sqlite_fixture_drill,
    sequence_reset_statements,
)


def test_sqlite_fixture_drill_keeps_events_across_rollback_and_restore(tmp_path: Path) -> None:
    report = run_sqlite_fixture_drill(tmp_path)
    assert report["ok"] is True
    assert report["backend"] == "sqlite-fixture"
    assert report["events_before"] >= 1
    assert report["events_after"] == report["events_before"]
    assert report["events_after_schema_rollback"] == report["events_before"]
    assert report["lost_events"] == []


def test_sequence_reset_rejects_unknown_tables() -> None:
    statement = sequence_reset_statements("task_events")
    assert "task_events" in statement
    assert "setval" in statement.lower()
    with pytest.raises(ValueError):
        sequence_reset_statements("users")


def test_pg_dump_command_keeps_password_out_of_argv_and_logs() -> None:
    argv, env = build_pg_dump_command(
        "postgresql://max:supersecretpass@db.internal:5432/max",
        Path("/tmp/max-agent-backup.dump"),
    )
    rendered = " ".join(argv)
    assert "supersecretpass" not in rendered
    assert "postgresql://max@db.internal:5432/max" in rendered
    assert env["PGPASSWORD"] == "supersecretpass"
    logged = format_command_for_log(argv, env)
    assert "supersecretpass" not in logged
    assert "PGPASSWORD" not in logged


@pytest.mark.skipif(
    not os.environ.get("MAX_AGENT_PG_DRILL"),
    reason="Live Postgres drill skipped: set MAX_AGENT_PG_DRILL to a postgresql URL. SQLite fixture covers event restore.",
)
def test_live_postgres_drill(tmp_path: Path) -> None:
    from max_agent.ops.backup import run_live_postgres_drill

    report = run_live_postgres_drill(os.environ["MAX_AGENT_PG_DRILL"], tmp_path)
    assert report["ok"] is True
    assert report["events_after"] == report["events_before"]
    assert "supersecret" not in str(report)
