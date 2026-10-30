# Worker daemon operations

`max-agent worker` continuously claims ready tasks, executes them, renews their leases, and finalizes the result. Task producers can return after `create`; they do not need to call `run` or `run-queue`.

This runbook covers a local or small private installation. It does not deploy a system service, open a public endpoint, enable external coding CLIs, or change approval gates.

## Run the API and worker separately

Use the same checkout and database for both processes. Initialize a new database, or apply migrations to an existing one, before starting either process:

```bash
export MAX_AGENT_DATABASE_URL=sqlite:////workspace/max-agent/data/max_agent.db
uv run --no-sync max-agent init-db
# For an existing database, use the normal migration path instead:
uv run --no-sync alembic upgrade head
```

Start the API only on loopback or an authenticated private endpoint:

```bash
export MAX_AGENT_API_TOKEN='<local-secret>'
export MAX_AGENT_API_ACTOR=user
uv run --no-sync max-agent serve --host 127.0.0.1 --port 8080
```

In a second terminal, use the same `MAX_AGENT_DATABASE_URL` and start the worker:

```bash
export MAX_AGENT_DATABASE_URL=sqlite:////workspace/max-agent/data/max_agent.db
uv run --no-sync max-agent worker \
  --concurrency 2 \
  --poll-interval 1 \
  --max-poll-interval 30 \
  --lease-ttl 600
```

The default heartbeat interval is one third of the lease TTL (200 seconds in this example). Set `--heartbeat-interval` explicitly when needed; it must be shorter than `--lease-ttl`. `--worker-id` can provide a stable lease-owner name, but each concurrently running worker must have a different ID. The default includes the host, PID, and a random suffix.

By default the worker can claim tasks for every owner. Use `--created-by user` to restrict it to the API principal's tasks. The worker accesses the database directly and does not need the API bearer token.

Create a task through the normal CLI or authenticated API. Do not call `run`; the worker picks it up automatically. Existing approval, side-effect replay, external-CLI authorization, and task-ownership rules still apply.

## Shutdown and recovery

Send `SIGTERM` or use Ctrl-C (`SIGINT`):

```bash
kill -TERM <WORKER_PID>
```

The worker stops claiming new tasks and waits for active attempts to finish and finalize. It does not forcibly kill worker threads or their unfinished external effects.

After an abrupt `SIGKILL` or process crash, the committed lease remains until it expires. This worker or another one then applies the existing recovery policy and fencing rules. A recovered attempt may enter retry backoff rather than run immediately. Non-idempotent side-effecting work remains blocked instead of being automatically replayed unless the existing approval/idempotency policy explicitly permits it.

## Concurrency, backoff, and logs

- `--concurrency` limits active attempts in this process. The scheduler also applies its in-flight lease cap in the database. SQLite still serializes writers; use PostgreSQL for larger multi-worker deployments.
- Empty-queue and transient-error delays start at `--poll-interval` and double up to `--max-poll-interval`. Successful work resets the delay. Shutdown interrupts an idle wait.
- Logs are newline-delimited JSON on stderr. They include worker/task IDs, statuses, backoff delays, renewal failures, and shutdown events. Known secret values are passed through the existing log redaction filter.
- The worker reuses the in-process telemetry counters. Its counters are included in the final shutdown log; they are not shared with the separate API process's `/v1/telemetry` endpoint.

Useful options are available from:

```bash
uv run --no-sync max-agent worker --help
```
