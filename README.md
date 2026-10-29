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
uv run max-agent list

# Local code worker (writes under /tmp + pytest)
uv run max-agent create "write adder" \
  -c worker_ok -c 'file_exists:/tmp/max_agent_mvp_demo.py' \
  -w local_code \
  --payload '{"action":"write_and_pytest","path":"/tmp/max_agent_mvp_demo.py","test_path":"/tmp/test_max_agent_mvp_demo.py"}'

# Side-effect retry needs confirmation
uv run max-agent run <TASK_ID> --confirm-side-effects
```

## HTTP API

```bash
uv run max-agent serve --host 127.0.0.1 --port 8080
# GET  /v1/health
# POST /v1/tasks
# GET  /v1/tasks/{id}
# POST /v1/tasks/{id}/schedule
# POST /v1/tasks/{id}/cancel
# GET  /v1/tasks/{id}/events
```

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
- [Release workflow](.github/workflows/release.yml): manually tagged versions get tested and packaged as GitHub Releases. Production deploy remains manual.

## Safety

Remote changes, paid APIs, and external side effects require authorization. The repository is currently private; GitHub branch protection may require an upgraded plan, so human review and CI confirmation remain mandatory.
