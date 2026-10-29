"""Logical snapshots, schema rollback helpers, and the event restore drill.

Production PostgreSQL backups still use pg_dump. The default drill uses a
throwaway SQLite file so CI can prove journal rows survive wipe and restore
without a database server.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import MetaData, select, text
from sqlalchemy.engine import Engine

from max_agent.db.session import get_engine, reset_engine

ROOT = Path(__file__).resolve().parents[3]
TABLE_ORDER = (
    "tasks",
    "task_events",
    "task_attempts",
    "aer_exports",
    "inbound_deliveries",
)
_SEQUENCE_TABLES = ("task_events", "aer_exports")


def sequence_reset_statements(table: str) -> str:
    if table not in _SEQUENCE_TABLES:
        raise ValueError(f"unknown sequence table {table}")
    return (
        "SELECT CASE WHEN pg_get_serial_sequence('"
        + table
        + "', 'id') IS NULL THEN NULL ELSE setval(pg_get_serial_sequence('"
        + table
        + "', 'id'), GREATEST(COALESCE((SELECT MAX(id) FROM "
        + table
        + "), 1), 1), true) END"
    )


def build_pg_dump_command(url: str, dest: Path) -> tuple[list[str], dict[str, str]]:
    parts = urlsplit(url)
    password = parts.password or ""
    host = parts.hostname or ""
    user = parts.username or ""
    if host:
        netloc = f"{user}@{host}" if user else host
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
    else:
        netloc = user
    scheme = parts.scheme.split("+", 1)[0]
    safe = urlunsplit((scheme, netloc, parts.path, parts.query, ""))
    argv = ["pg_dump", "--format=custom", "--file", str(dest), "--dbname", safe]
    env: dict[str, str] = {}
    if password:
        env["PGPASSWORD"] = password
    return argv, env


def format_command_for_log(argv: list[str], env: dict[str, str]) -> str:
    """Argv only. `env` is accepted so callers do not splice it into the log line."""
    del env
    return " ".join(argv)


def _encode(value: object) -> object:
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    return value


def _decode(value: object) -> object:
    if isinstance(value, dict) and set(value.keys()) == {"$datetime"}:
        return datetime.fromisoformat(str(value["$datetime"]))
    return value


def _reflect(engine: Engine) -> MetaData:
    meta = MetaData()
    meta.reflect(bind=engine)
    return meta


def export_snapshot(engine: Engine) -> dict[str, object]:
    meta = _reflect(engine)
    tables: dict[str, list[dict[str, object]]] = {}
    with engine.connect() as conn:
        for name in TABLE_ORDER:
            if name not in meta.tables:
                continue
            table = meta.tables[name]
            rows: list[dict[str, object]] = []
            for mapping in conn.execute(select(table)).mappings():
                rows.append({key: _encode(val) for key, val in mapping.items()})
            tables[name] = rows
    return {"format": "max-agent-snapshot-v1", "tables": tables}


def wipe_data(engine: Engine) -> None:
    meta = _reflect(engine)
    with engine.begin() as conn:
        for name in reversed(TABLE_ORDER):
            if name in meta.tables:
                conn.execute(meta.tables[name].delete())


def restore_snapshot(engine: Engine, snapshot: dict[str, object]) -> None:
    if snapshot.get("format") != "max-agent-snapshot-v1":
        raise ValueError("unsupported snapshot")
    raw_tables = snapshot.get("tables") or {}
    if not isinstance(raw_tables, dict):
        raise ValueError("unsupported snapshot")
    meta = _reflect(engine)
    with engine.begin() as conn:
        for name in reversed(TABLE_ORDER):
            if name in meta.tables:
                conn.execute(meta.tables[name].delete())
        for name in TABLE_ORDER:
            if name not in meta.tables:
                continue
            rows = raw_tables.get(name) or []
            if not isinstance(rows, list) or not rows:
                continue
            decoded = []
            for row in rows:
                if isinstance(row, dict):
                    decoded.append({key: _decode(val) for key, val in row.items()})
            if decoded:
                conn.execute(meta.tables[name].insert(), decoded)
        if engine.dialect.name == "postgresql":
            for name in _SEQUENCE_TABLES:
                if name in meta.tables:
                    conn.execute(text(sequence_reset_statements(name)))


def event_fingerprints(engine: Engine) -> list[tuple[object, ...]]:
    meta = _reflect(engine)
    if "task_events" not in meta.tables:
        return []
    table = meta.tables["task_events"]
    found: list[tuple[object, ...]] = []
    with engine.connect() as conn:
        rows = conn.execute(
            select(table.c.id, table.c.task_id, table.c.event_type, table.c.message, table.c.payload).order_by(
                table.c.id
            )
        ).all()
    for row in rows:
        payload = row.payload
        if isinstance(payload, str):
            payload = json.loads(payload)
        found.append(
            (
                row.id,
                row.task_id,
                row.event_type,
                row.message,
                json.dumps(payload, sort_keys=True, default=str),
            )
        )
    return found


def _alembic_config(url: str):  # type: ignore[no-untyped-def]
    from alembic.config import Config

    os.environ["MAX_AGENT_DATABASE_URL"] = url
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade_database(url: str, revision: str) -> None:
    from alembic import command

    command.upgrade(_alembic_config(url), revision)


def downgrade_database(url: str, revision: str) -> None:
    from alembic import command

    command.downgrade(_alembic_config(url), revision)


def _seed_events(url: str) -> None:
    from max_agent.db.models import TaskEventRow, TaskRow
    from max_agent.db.session import session_scope

    reset_engine()
    task_id = str(uuid.uuid4())
    with session_scope(url) as session:
        session.add(
            TaskRow(
                id=task_id,
                goal="restore drill",
                acceptance_criteria=["worker_ok"],
                status="pending",
                worker_kind="mock",
                worker_payload={"mode": "success"},
                dependencies=[],
                tags=[],
                approval_scope=[],
                budget_ceilings={},
            )
        )
        session.add(
            TaskEventRow(
                task_id=task_id,
                event_type="created",
                message="keep-me",
                payload={"marker": "event-1"},
            )
        )
        session.add(
            TaskEventRow(
                task_id=task_id,
                event_type="noted",
                message="also-keep",
                payload={"marker": "event-2"},
            )
        )
    reset_engine()


def _open(url: str) -> Engine:
    reset_engine()
    os.environ["MAX_AGENT_DATABASE_URL"] = url
    return get_engine(url)


def assert_disposable_database(url: str) -> None:
    name = urlsplit(url).path.rsplit("/", 1)[-1].lower()
    if "drill" not in name and "test" not in name:
        raise ValueError("refusing to drill a database whose name does not contain 'drill' or 'test'")


def _drill_report(url: str, *, backend: str) -> dict[str, object]:
    upgrade_database(url, "head")
    _seed_events(url)
    before = event_fingerprints(_open(url))
    downgrade_database(url, "-1")
    after_schema = event_fingerprints(_open(url))
    upgrade_database(url, "head")
    engine = _open(url)
    before_restore = event_fingerprints(engine)
    snapshot = export_snapshot(engine)
    wipe_data(engine)
    if event_fingerprints(engine):
        raise RuntimeError("wipe did not remove events")
    restore_snapshot(engine, snapshot)
    after = event_fingerprints(_open(url))
    lost = [list(item) for item in before_restore if item not in after]
    ok = not lost and len(after_schema) == len(before) and len(after) == len(before) and len(before) >= 1
    return {
        "ok": ok,
        "backend": backend,
        "events_before": len(before),
        "events_after_schema_rollback": len(after_schema),
        "events_after": len(after),
        "lost_events": lost,
        "live_postgres": backend == "postgres",
    }


def run_database_drill(url: str, *, backend: str) -> dict[str, object]:
    return _drill_report(url, backend=backend)


def run_sqlite_fixture_drill(workdir: Path) -> dict[str, object]:
    workdir.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{workdir / 'drill.db'}"
    previous = os.environ.get("MAX_AGENT_DATABASE_URL")
    try:
        return _drill_report(url, backend="sqlite-fixture")
    finally:
        reset_engine()
        if previous is None:
            os.environ.pop("MAX_AGENT_DATABASE_URL", None)
        else:
            os.environ["MAX_AGENT_DATABASE_URL"] = previous


def run_live_postgres_drill(url: str, workdir: Path) -> dict[str, object]:
    if not url.startswith("postgresql"):
        raise ValueError("live drill requires a postgresql URL")
    assert_disposable_database(url)
    workdir.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("MAX_AGENT_DATABASE_URL")
    try:
        report = _drill_report(url, backend="postgres")
        return report
    finally:
        reset_engine()
        if previous is None:
            os.environ.pop("MAX_AGENT_DATABASE_URL", None)
        else:
            os.environ["MAX_AGENT_DATABASE_URL"] = previous
