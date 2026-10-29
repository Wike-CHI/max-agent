# Test Evidence

Captured: 2026-10-29 17:13:02 CST (Asia/Shanghai)

## Environment (brief)

```
Python 3.13.5
uv 0.13.5 (x86_64-unknown-linux-gnu)
git version 2.47.3
```

## pytest

```
$ cd /workspace/max-agent && uv run pytest -q
/workspace/max-agent/.venv/lib/python3.13/site-packages/pytest_asyncio/plugin.py:300: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the "fixture" caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
.............                                                            [100%]
=============================== warnings summary ===============================
.venv/lib/python3.13/site-packages/fastapi/testclient.py:1
  /workspace/max-agent/.venv/lib/python3.13/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
13 passed, 1 warning in 1.06s
```

## Acceptance demo (key outcomes)

- Create mock task → persisted, status `ready` → run → `succeeded`
- Journal events include created / status_changed / attempt_* / tool_call / verification
- LocalCodeWorker write+pytest → `succeeded` (`/tmp/max_agent_mvp_demo.py`)
- Recoverable mock failure → `retry_wait` with last_error
- New process query after reset_engine → prior tasks/events still readable

## Commands

```bash
export MAX_AGENT_DB_PATH=/workspace/max-agent/data/acceptance.db
uv run max-agent init-db
uv run max-agent create "..." -c worker_ok -w mock --payload '{"mode":"success","answer":"42"}' -k accept-mock-1
uv run max-agent run <TASK_ID>
uv run max-agent events <TASK_ID>
bash scripts/acceptance_demo.sh
```

## Issue #5 Grok entry (2026-10-29)

Local commands on Python 3.12.3 / uv 0.13.6:

```bash
uv sync --locked --extra dev
uv run --no-sync pytest -q
uv build
uv run --no-sync python scripts/grok_entry_demo.py
```

`pytest -q`: 40 passed on CPython 3.12.3 and again on 3.13.18. One pre-existing Starlette/`httpx` warning.

`uv build`: `dist/max_agent-0.1.0.tar.gz` and `dist/max_agent-0.1.0-py3-none-any.whl`.

Demo (`caller_poll_loops` is 0). The bot called `create`, then one worker `run`, then one `status`. Publish stayed blocked until `approve`. Webhook replay was deduplicated and a second delivery id hit the same task.

```json
{
  "caller_poll_loops": 0,
  "create": {"status": "ready", "attempts": 0},
  "worker_run_status": "succeeded",
  "status_once": {
    "status": "succeeded",
    "verification": {"passed": true}
  },
  "approval": {
    "before": "blocked",
    "run_without_approve": "blocked",
    "after_approve": "ready",
    "after_approved_run": "succeeded"
  },
  "webhook": {
    "replay_deduplicated": true,
    "same_task": true,
    "associated_idempotent_hit": true,
    "associated_same_task": true
  }
}
```

## v0.4 CLI worker (Issue #4)

Captured: 2026-10-29. Host Python 3.12.3, uv 0.13.6. `grok` and `codex` were not on `PATH`, so real CLI processes were not started.

```
$ uv sync --locked --extra dev
$ uv run --no-sync pytest -q
58 passed, 1 warning in 11.31s
$ uv build
Successfully built dist/max_agent-0.1.0.tar.gz
Successfully built dist/max_agent-0.1.0-py3-none-any.whl
```

The pre-existing warning is Starlette's `httpx` TestClient deprecation. It is not from the CLI worker.

CLI smoke (sqlite file under `/tmp`, vendor `auto`, no `MAX_AGENT_ALLOW_EXTERNAL_CLI`):

- create `ce943e27-7533-4607-8715-43279e088929` status `ready`, attempts 0
- run status `succeeded`, worker `cli`, attempts 1, summary `cli fixture verified`

The wheel contains `max_agent/workers/fixtures/cli_issue/ISSUE.md`.
