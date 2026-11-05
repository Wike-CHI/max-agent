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

## Require a real CLI worker

For production or acceptance CLI tasks, set `worker_payload.require_real_worker` to `true` (string values `1`, `true`, and `yes` are also accepted), or export `MAX_AGENT_REQUIRE_REAL_WORKER=1` in the worker process. The adapter rejects explicit `stub` and `auto` stub fallback with `adapter_failure: real_worker_required` before creating a worktree. Verification and finalization also refuse terminal `succeeded` when a CLI report contains `real_cli_invoked` but its value is not `true`. Reports from non-CLI mock workers do not contain that key and retain their existing behavior.

Real execution still requires `MAX_AGENT_ALLOW_EXTERNAL_CLI=1` and a successful vendor version probe. Always inspect the worker output for `vendor`, `real_cli_invoked`, `adapter_failure`, and `require_real_worker`; do not treat stub success as external-CLI acceptance. Leaving the requirement unset preserves the local stub demo for development and CI.

An isolated worktree is only a separate checkout, not OS, user, container, or VM isolation. CLI sandbox flags do not change that boundary.

Compatible Anthropic/OpenAI endpoints (for example MiniMax Claude) need their `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` or `OPENAI_BASE_URL` present in the host worker environment; the CliWorker sandbox allowlist passes those through to the child when set, and omits them when unset. Database URLs and GitHub tokens still never reach the child.

## Model tier (`cheap` / `strong` / `auto`)

A task may set `worker_payload.model_tier` to `cheap`, `strong`, or `auto`. Most tasks should use `cheap` or `auto` (about 80% of default work). `strong` is opt-in and is the only tier that prefers Codex / GPT Pro class CLIs. Omit the field to keep today's vendor selection.

| `model_tier` | `vendor=auto` order | Endpoint and model |
| --- | --- | --- |
| omitted | Grok, then Codex, then Claude | Unchanged historical `auto` |
| `cheap` | Claude, then Grok. Codex is not a candidate | Claude child receives allowlisted `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY`, and `ANTHROPIC_MODEL` / `CLAUDE_CODE_SUBAGENT_MODEL` when the worker process has them. Point `ANTHROPIC_BASE_URL` at a compatible endpoint such as MiniMax |
| `auto` | Same as `cheap` | Same env as `cheap`. This does not escalate after pytest |
| `strong` | Codex, then Grok, then Claude | Codex child receives allowlisted `OPENAI_API_KEY` and `OPENAI_BASE_URL` when set |

An explicit `vendor` other than `auto` (`stub`, `grok`, `codex`, or `claude`) still wins. The tier is recorded as `model_tier` and does not reorder that vendor. Worker output also lists `route_vendors` for the order that was considered.

Any other value fails before a worktree or stub run: `adapter_failure` is `invalid_model_tier`, `cli_invoked` is false, and the summary names the rejected value. That failure is not retried as a stub success.

`MAX_AGENT_ALLOW_EXTERNAL_CLI` unset does not start a real CLI. `vendor=auto` with a valid tier still uses the stub, and `require_real_worker` still returns `real_worker_required`. The tier does not open the external-CLI gate.

MAX does not put `--model` or a provider URL on the CLI argv. [cc-switch](https://github.com/farion1231/cc-switch) is the intended controller for provider and model: in its routing mode it points Claude's `ANTHROPIC_BASE_URL` at the local route (default `http://127.0.0.1:15721`) from the app settings file. This adapter does not spawn cc-switch and does not read `~/.claude/settings.json`. The child `HOME` is the isolated worktree home, so that settings file is not visible to the CLI. Cooperation is the allowlisted environment above: export the endpoint and model cc-switch selected into the worker process. Do not hardcode a MiniMax URL or a GPT Pro model name in MAX. A later change can ask cc-switch to apply a cheap or strong profile; this ticket does not.

Pytest failure does not switch a run to `strong`. That escalate is a separate change.

## Model and usage on each CLI attempt

A real CLI attempt records which vendor and model ran. Worker output includes `vendor`, `model`, and `provider`. The same `model` and `provider` are stored on `task_attempts` and shown by `max-agent trace <id>` and by the AER record for `attempt_finished`.

Claude takes the model from CLI JSON when that JSON includes one, then from `ANTHROPIC_MODEL`, then from `CLAUDE_CODE_SUBAGENT_MODEL`. Grok and Codex use a model field in CLI JSON when present. `provider` is the JSON `provider` when set, otherwise `xai`, `openai`, or `anthropic` for those three vendors. Stub runs, and any name the CLI did not report, use the string `unknown`. The adapter does not fill a missing model or cost with `0`.

When CLI JSON has `usage.marker` of `measured` or `estimate` plus an amount, that report is written to `task_attempts.cost_source`, `cost_marker`, `cost_amount`, `cost_currency`, `input_tokens`, and `output_tokens`. A vendor cost field such as `total_cost_usd` is stored as `measured` on the `api_spend` ledger. Token counts are kept only with that report. If the CLI reports no cost, those cost fields stay `unknown` or empty. Recording usage does not escalate a run. Tier selection is the `model_tier` field above; a pytest failure does not switch tiers. An isolated worktree is still not OS, user, container, or VM isolation.

For a local gate check or an operator-authorized live drill, run `scripts/real_cli_e2e_drill.sh` and see [evidence/real-cli-e2e-2026-10-31.md](evidence/real-cli-e2e-2026-10-31.md).

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
