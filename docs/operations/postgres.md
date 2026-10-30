# PostgreSQL migration rollback, backup, and restore

SQLite remains the default for local runs and CI. PostgreSQL is the concurrent runtime (`FOR UPDATE SKIP LOCKED` is already the claim path when the URL is Postgres). This repo does not ship a running Postgres service.

## Order of operations

1. Take a backup **before** `alembic upgrade`.
2. Upgrade: `uv run alembic upgrade head`.
3. Run the staging smoke test.
4. On failure, stop writers, then either roll the schema back or restore the backup. Read that revision's `downgrade()` first. A downgrade that only drops new columns can keep `task_events`. A downgrade that drops tables cannot. When in doubt, restore the backup.
5. Re-run the drill and confirm event fingerprints match.

The fixture drill runs `alembic downgrade -1` and then upgrades again. On this branch that revision drops the `created_by` ownership column and its index from `tasks` and does not delete `task_events`. The drill asserts the events are still there. Reapplying the migration assigns existing rows to the default `user` principal.

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

## Live drill and archived evidence

Set `MAX_AGENT_PG_DRILL` to a disposable Postgres URL whose database name contains `drill` or `test`. Install the locked optional driver, then run the evidence runner:

```bash
uv sync --locked --extra dev --extra postgres
MAX_AGENT_PG_DRILL=postgresql://max@localhost:5432/max_agent_drill \
  uv run --no-sync --extra postgres python scripts/postgres_restore_drill.py
```

The default output is `docs/operations/evidence/postgres-restore-YYYY-MM-DD.json`; use `--output PATH` to choose another file. The evidence contains the source revision, event counts, lost-event list, and pass/fail status, but never the database URL or password. A nonzero exit means the live drill failed.

Do not point the drill at a database you need to keep: it downgrades the latest revision, deletes rows, and restores the snapshot. When `MAX_AGENT_PG_DRILL` is unset, the runner archives `status: skipped` and exits successfully; this is not evidence of a Postgres pass.

The same live path is available through the opt-in test:

```bash
MAX_AGENT_PG_DRILL=postgresql://max@localhost:5432/max_agent_drill \
  uv run --no-sync --extra postgres pytest -q tests/test_backup_restore.py::test_live_postgres_drill
```

Current evidence: [postgres-restore-2026-10-30.json](evidence/postgres-restore-2026-10-30.json). It records a passing live PostgreSQL drill with 2 events before the schema rollback, 2 after it, and 2 after restore; `lost_events` is empty. This satisfies the evidence requirement in v1.0 exit criterion #4, but does not by itself declare or approve v1.0.
