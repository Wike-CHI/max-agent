# MAX-Agent

Task-centric personal agent scheduler kernel.

**Task First, Agent as Worker.**

## Requirements

- Python 3.12+ (tested on 3.13)
- `uv` recommended (or pip + venv)

## Install

```bash
cd /workspace/max-agent
uv sync --extra dev
# or: python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

## Initialize DB

```bash
export MAX_AGENT_DB_PATH=/workspace/max-agent/data/max_agent.db
uv run max-agent init-db
# optional migrations:
uv run alembic upgrade head
```

## CLI examples

```bash
# Create a mock task
uv run max-agent create "echo hello" \
  -c worker_ok -c 'output_equals:answer=hello' \
  -w mock --payload '{"mode":"success","answer":"hello"}' \
  -k demo-1

# Run it
uv run max-agent run <TASK_ID>

# After a crash, expired leases are requeued. Side effects without an
# idempotency key stay blocked until --confirm-side-effects.
uv run max-agent recover

# Inspect
uv run max-agent get <TASK_ID>
uv run max-agent events <TASK_ID>
uv run max-agent trace <TASK_ID>
uv run max-agent metrics
uv run max-agent aer-retry
uv run max-agent list

# Optional cost ceiling and issue ref. 0 aggregate cost means "unset".
# --ceiling source is grok_bot_quota, cli_quota, or api_spend.
uv run max-agent create "echo hello" -c worker_ok --issue Wike-CHI/max-agent#6 \
  --max-cost 1 --ceiling api_spend=0.5

# Local code worker (writes under /tmp + pytest)
uv run max-agent create "write adder" \
  -c worker_ok -c 'file_exists:/tmp/max_agent_mvp_demo.py' \
  -w local_code \
  --payload '{"action":"write_and_pytest","path":"/tmp/max_agent_mvp_demo.py","test_path":"/tmp/test_max_agent_mvp_demo.py"}'

# CLI worker: isolated worktree + stub. Does not call Grok or Codex.
uv run max-agent create "implement add" \
  -c worker_ok -c output_key:adapter_version \
  -w cli \
  --payload '{"vendor":"auto","workspace_root":"/tmp/max-agent-workspaces"}'

# Side-effect retry needs confirmation
uv run max-agent run <TASK_ID> --confirm-side-effects
```

## Grok Bot entry

Grok is the caller. MAX keeps the task. The bot creates a task, stores `task_id`, and reads status later. It does not sit in a poll loop, and it does not decide success.

Machine callers pass `--json` before the subcommand. The envelope always has the same keys (`ok`, `command`, `task_id`, `status`, `approval_required`, `approved`, `verification`, `events`, ...).

```bash
uv run max-agent --json create "echo hello" \
  -c worker_ok -c 'output_equals:answer=hello' \
  -w mock --payload '{"mode":"success","answer":"hello"}' \
  -k grok-1

uv run max-agent --json status <TASK_ID>
uv run max-agent --json events <TASK_ID>
uv run max-agent --json cancel <TASK_ID>

# publish / payment / external_write stay waiting_approval until this returns
uv run max-agent --json approve <TASK_ID> --by "user" --note "ok"
```

`run` is the worker process (one attempt). A local mock or `local_code` task does not need production credentials. Instructions for a Grok Bot: [docs/skills/grok-max-entry/SKILL.md](docs/skills/grok-max-entry/SKILL.md).

```bash
uv run python scripts/grok_entry_demo.py
uv run max-agent mcp   # stdio JSON-RPC: initialize, tools/list, tools/call
```

## HTTP API

```bash
uv run max-agent serve --host 127.0.0.1 --port 8080
# GET  /v1/health                 # liveness: process up, version
# GET  /v1/ready                  # readiness: SELECT 1
# GET  /v1/telemetry              # in-process HTTP spans and counters
# POST /v1/tasks
# GET  /v1/tasks/{id}
# GET  /v1/tasks/{id}/status          # same envelope as `max-agent --json status`
# POST /v1/tasks/{id}/schedule
# POST /v1/tasks/{id}/cancel
# POST /v1/tasks/graph                # {"nodes":[{"key":"a","goal":"...","depends_on":[]}]}
# POST /v1/queue/run?limit=50
# POST /v1/tasks/{id}/approve         # {"actor":"user","note":"...","scope":[],"ttl_seconds":null}
# POST /v1/tasks/{id}/reject          # {"actor":"user","note":"..."}
# GET  /v1/tasks/{id}/events
# GET  /v1/tasks/{id}/trace           # issue -> worker -> verification -> pull request
# GET  /v1/metrics                     # verified_task_rate, cost_per_verified_task, retry_overhead
# POST /v1/aer/retry                   # redeliver failed AER exports; does not run tasks
# POST /v1/inbound/webhook            # signed Routine/webhook create; returns task_id
```

Webhook requests need `MAX_AGENT_WEBHOOK_SECRET` plus headers `X-Max-Timestamp`, `X-Max-Signature` (hex HMAC-SHA256 of `{timestamp}.{raw_body}`), and `X-Max-Delivery-Id`. Replays of the same delivery id return the original task. A new delivery with the same `idempotency_key` attaches to that task and does not start a second one. The handler does not run the worker.

## Tests

```bash
cd /workspace/max-agent
uv run pytest -q
```

## Docs

- `docs/env-survey.md` — Phase 0 environment findings
- `docs/architecture.md` — architecture (bilingual notes)
- `docs/limitations.md` — known limits + next phase
- `docs/test-evidence.md` — captured pytest / demo output

## GitHub delivery workflow

- [Product roadmap](docs/ROADMAP.md): v0.1 MVP → Grok entry → durable execution → real workers → cost and orchestration.
- [Contribution / Issue–PR protocol](CONTRIBUTING.md): Issue → branch → PR → CI → squash merge → reviewed version tag.
- [CI workflow](.github/workflows/ci.yml): issue-link, Python 3.12/3.13 tests, CLI startup and package build.
- [Release workflow](.github/workflows/release.yml): manually tagged versions get tested, packaged, checksummed, and published as GitHub Releases. The provenance file is an unsigned SHA256 predicate.
- [Operations runbook](docs/operations/README.md): supply-chain audit, restore drill, staging smoke, production approval gate, health/SLO, and the branch-protection fallback. Production deploy remains manual.

## Safety

Remote changes, paid APIs, and external side effects require authorization. The repository is currently private; GitHub branch protection may require an upgraded plan, so human review and CI confirmation remain mandatory.
