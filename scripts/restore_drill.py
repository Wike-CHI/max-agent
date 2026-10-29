"""Run the journal restore drill, or export/restore a logical snapshot.

The default path builds a throwaway SQLite database, rolls the latest Alembic
revision down and up, wipes rows, and restores them. Live Postgres is opt-in
and only accepted when the database name contains 'drill' or 'test'.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from max_agent.ops.backup import (
    assert_disposable_database,
    export_snapshot,
    restore_snapshot,
    run_database_drill,
    run_live_postgres_drill,
    run_sqlite_fixture_drill,
)
from max_agent.db.session import get_engine, reset_engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAX-Agent backup, restore, and event drill.")
    parser.add_argument("--database-url", default="")
    parser.add_argument("--export", type=Path)
    parser.add_argument("--restore", type=Path)
    parser.add_argument("--drill", action="store_true")
    args = parser.parse_args(argv)

    if not args.database_url and not args.export and not args.restore and not args.drill:
        work = Path(tempfile.mkdtemp(prefix="max-drill-"))
        report = run_sqlite_fixture_drill(work)
        print(json.dumps(report))
        return 0 if report["ok"] else 1

    if not args.database_url:
        print("database url is required for export, restore, or a named drill", file=sys.stderr)
        return 2

    if args.database_url.startswith("postgresql"):
        try:
            assert_disposable_database(args.database_url)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if args.drill or (not args.export and not args.restore):
        if args.database_url.startswith("postgresql"):
            report = run_live_postgres_drill(args.database_url, Path(tempfile.mkdtemp(prefix="max-pg-drill-")))
        elif args.database_url.startswith("sqlite"):
            report = run_database_drill(args.database_url, backend="sqlite")
        else:
            print("unsupported database url scheme", file=sys.stderr)
            return 2
        print(json.dumps(report))
        return 0 if report["ok"] else 1

    os.environ["MAX_AGENT_DATABASE_URL"] = args.database_url
    reset_engine()
    engine = get_engine(args.database_url)
    if args.export:
        snapshot = export_snapshot(engine)
        args.export.write_text(json.dumps(snapshot))
        print(f"exported logical snapshot to {args.export.name}")
        return 0
    if args.restore:
        snapshot = json.loads(args.restore.read_text())
        restore_snapshot(engine, snapshot)
        print(f"restored logical snapshot from {args.restore.name}")
        return 0
    print("nothing to do", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
