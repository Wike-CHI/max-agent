# PostgreSQL migration rollback, backup, and restore

SQLite remains the default for local runs and CI. PostgreSQL is the concurrent runtime (`FOR UPDATE SKIP LOCKED` is already the claim path when the URL is Postgres). This repo does not ship a running Postgres service.

## Order of operations

1. Take a backup **before** `alembic upgrade`.
2. Upgrade: `uv run alembic upgrade head`.
3. Run the staging smoke test.
4. On failure, stop writers, then either roll the schema back or restore the backup. Read that revision's `downgrade()` first. A downgrade that only drops new columns can keep `task_events`. A downgrade that drops tables cannot. When in doubt, restore the backup.
5. Re-run the drill and confirm event fingerprints match.

The fixture drill runs `alembic downgrade -1` and then upgrades again. On this branch that revision drops approval columns on `tasks` and does not delete `task_events`. The drill asserts the events are still there.

## Logical snapshot (no pg_dump required)

```bash
uv run --no-sync python scripts/restore_drill.py
```

With no arguments this creates a temporary SQLite database, upgrades to head, writes two journal events, runs `alembic downgrade -1` and `upgrade head`, wipes rows, restores the snapshot, and prints JSON. `events_after` must equal `events_before`, and `lost_events` must be empty. CI runs this command. It is the proof used when Postgres is not installed.

Export or restore a URL you already have:

```bash
uv run --no-sync python scripts/restore_drill.py \
  --database-url "$MAX_AGENT_DATABASE_URL" \
  --export backup.json

uv run --no-sync python scripts/restore_drill.py \
  --database-url "$MAX_AGENT_DATABASE_URL" \
  --restore backup.json
```

Restore refuses a Postgres URL whose database name does not contain `drill` or `test`.

## pg_dump

When `pg_dump` / `pg_restore` are installed, take a custom-format dump **before** migrating. Keep the password in `PGPASSWORD` or a `.pgpass` file, not in the command line and not in the shell history you paste into logs.

```bash
pg_dump --format=custom --file=backup.dump --dbname=postgresql://USER@HOST:5432/DBNAME
# on failure, after stopping writers:
pg_restore --clean --if-exists --dbname=postgresql://USER@HOST:5432/DBNAME backup.dump
```

`build_pg_dump_command` in `max_agent.ops.backup` builds that argv and puts the password only in the environment.

## Live drill

Set `MAX_AGENT_PG_DRILL` to a disposable Postgres URL (database name contains `drill` or `test`) and install a SQLAlchemy driver such as `psycopg`. The default test run skips this path and says so. Do not point the drill at a database you need to keep: it downgrades the latest revision, deletes rows, and restores the snapshot.

```bash
MAX_AGENT_PG_DRILL=postgresql://max@localhost:5432/max_agent_drill \
  uv run --no-sync pytest -q tests/test_backup_restore.py::test_live_postgres_drill
```
