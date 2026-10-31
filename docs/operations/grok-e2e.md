# Grok Bot end-to-end drill checklist

Use this checklist on a disposable Grok Bot cloud computer to verify the real CLI, MCP, webhook, scheduler, and CLI-worker entry points. It exercises the application directly; it does not use `scripts/grok_entry_demo.py` or an in-process HTTP test client.

Recorded run: [evidence/grok-e2e-2026-10-30.md](evidence/grok-e2e-2026-10-30.md).

## Safety and acceptance rules

- [ ] Work on a disposable checkout and a dedicated database under `/tmp`.
- [ ] Bind HTTP only to `127.0.0.1`. Never expose an unauthenticated API publicly; remote access requires authentication plus TLS or a private tunnel.
- [ ] Use newly generated local-only API/webhook secrets. Do not use production credentials, commit secrets, print signatures, or paste tokens into evidence.
- [ ] Use only mock work for the basic entry-point checks. An `external_write` approval on a mock task does not authorize a real external write.
- [ ] Leave `MAX_AGENT_ALLOW_EXTERNAL_CLI` unset for the initial CLI-worker stub check.
- [ ] Only an operator may enable `MAX_AGENT_ALLOW_EXTERNAL_CLI=1` for a deliberate vendor probe. Never set it in `docs/skills/grok-max-entry/SKILL.md` or in the Grok Bot skill itself.
- [ ] Do not install a fake `grok`/`codex`/`claude` executable. A stub or `auto` fallback is not real external-CLI acceptance.
- [ ] Do not run a polling loop. `create` and webhook return immediately; one `run` invocation performs one worker attempt; query `status` and `events` on demand.
- [ ] Stop the loopback server and unset the external gate and secrets when finished.

Real external-CLI acceptance requires all of: an authorized real binary, successful version probe, `real_cli_invoked: true`, terminal `succeeded`, verification passed, and inspectable artifacts. A paid CLI is not required for the mock, webhook, MCP, or stub checks and must not be required in CI.

## 1. Prepare an isolated checkout

```bash
git status --short --branch
uv sync --locked --extra dev

export MAX_AGENT_DATABASE_URL=sqlite:////tmp/max-agent-grok-e2e.db
export MAX_AGENT_API_TOKEN="$(uv run --no-sync python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export MAX_AGENT_API_ACTOR=user
export MAX_AGENT_WEBHOOK_SECRET="$(uv run --no-sync python -c 'import secrets; print(secrets.token_urlsafe(32))')"
unset MAX_AGENT_ALLOW_EXTERNAL_CLI XAI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY
mkdir -p /tmp/max-agent-grok-e2e-evidence
```

Use a new database path, or remove `/tmp/max-agent-grok-e2e.db` only when it is known to be left by an earlier disposable drill. `MAX_AGENT_API_ACTOR=user` is aligned with the current webhook task owner; another actor receives 403 for that task even with the right bearer token.

Initialize through the real CLI:

```bash
uv run --no-sync python -m max_agent.cli.main --json init-db
```

Checklist:

- [ ] `uv sync` used the lockfile and completed successfully.
- [ ] The database is dedicated to this drill.
- [ ] The token and webhook secret are non-production values and are absent from logs.
- [ ] No external CLI gate or provider key is set yet.

## 2. Direct CLI mock task

```bash
uv run --no-sync python -m max_agent.cli.main --json create \
  'Grok e2e direct CLI' -c worker_ok -c output_equals:answer=cli-mock-ok \
  -w mock --payload '{"mode":"success","answer":"cli-mock-ok"}' \
  -k grok-e2e-cli-mock
```

Copy the returned `task_id`, then run exactly one worker attempt and read the persisted evidence:

```bash
uv run --no-sync python -m max_agent.cli.main --json run <TASK_ID>
uv run --no-sync python -m max_agent.cli.main --json status <TASK_ID>
uv run --no-sync python -m max_agent.cli.main --json events <TASK_ID>
```

- [ ] Create returned `ready` and `task.attempts: 0`.
- [ ] Final status is `succeeded` and `verification.passed` is `true`.
- [ ] Events include create, lease/schedule, attempt start/finish, worker output, and verification.
- [ ] Record the task ID, statuses, verification, and event-type summary in a dated evidence file.

## 3. Approval-gated mock task

Use an approval tag but keep the worker payload local and harmless:

```bash
uv run --no-sync python -m max_agent.cli.main --json create \
  'Grok e2e approval' -c worker_ok -c output_equals:answer=approved-ok \
  -w mock --payload '{"mode":"success","answer":"approved-ok"}' \
  --tag external_write -k grok-e2e-cli-approval
```

Confirm that the response is `waiting_approval` with `approval_required: true`. After the operator approves this **mock-only** action:

```bash
uv run --no-sync python -m max_agent.cli.main --json approve <TASK_ID> \
  --by grok-e2e-operator --note 'Local mock only; no external side effect' \
  --scope external_write
uv run --no-sync python -m max_agent.cli.main --json run <TASK_ID>
uv run --no-sync python -m max_agent.cli.main --json status <TASK_ID>
uv run --no-sync python -m max_agent.cli.main --json events <TASK_ID>
```

- [ ] Approval changed the task to `ready` with `approved: true` before the worker ran.
- [ ] Final status and verification are successful.
- [ ] The journal contains `approval_required` before `approved`.
- [ ] No real external action was requested or performed.

## 4. Real signed webhook over loopback HTTP

In terminal A, with the same environment, start the real server. Do not use `--host 0.0.0.0`:

```bash
uv run --no-sync python -m max_agent.cli.main serve --host 127.0.0.1 --port 18080
```

In terminal B, restore the same drill environment and create a raw JSON body outside the repository:

```bash
cat > /tmp/max-agent-grok-e2e-evidence/webhook-request.json <<'JSON'
{"idempotency_key":"grok-e2e-webhook-mock","goal":"Grok e2e webhook","acceptance_criteria":["worker_ok","output_equals:answer=webhook-ok"],"worker_kind":"mock","worker_payload":{"mode":"success","answer":"webhook-ok"}}
JSON
```

Check readiness, sign the exact bytes, and send one delivery. Do not print the secret or signature:

```bash
curl --fail http://127.0.0.1:18080/v1/ready
TIMESTAMP="$(date +%s)"
SIGNATURE="$(uv run --no-sync python -c '
import hashlib, hmac, os, sys
body = open("/tmp/max-agent-grok-e2e-evidence/webhook-request.json", "rb").read()
message = sys.argv[1].encode() + b"." + body
print(hmac.new(os.environ["MAX_AGENT_WEBHOOK_SECRET"].encode(), message, hashlib.sha256).hexdigest())
' "$TIMESTAMP")"
curl --fail -X POST http://127.0.0.1:18080/v1/inbound/webhook \
  -H 'Content-Type: application/json' \
  -H "X-Max-Timestamp: $TIMESTAMP" \
  -H "X-Max-Signature: $SIGNATURE" \
  -H 'X-Max-Delivery-Id: grok-e2e-delivery-1' \
  --data-binary @/tmp/max-agent-grok-e2e-evidence/webhook-request.json
```

Use the returned task ID for authenticated HTTP and CLI checks:

```bash
curl -H "Authorization: Bearer $MAX_AGENT_API_TOKEN" \
  http://127.0.0.1:18080/v1/tasks/<TASK_ID>/status
uv run --no-sync python -m max_agent.cli.main --json run <TASK_ID>
uv run --no-sync python -m max_agent.cli.main --json status <TASK_ID>
uv run --no-sync python -m max_agent.cli.main --json events <TASK_ID>
```

- [ ] Readiness succeeded over loopback HTTP.
- [ ] Webhook returned HTTP 200, `ready`, and `attempts: 0`; it did not run the worker.
- [ ] Authenticated status returned HTTP 200; an omitted/invalid token must return 401.
- [ ] Final status and verification succeeded after one worker invocation.
- [ ] Events include `inbound` and the normal attempt/verification trail.
- [ ] Stop terminal A with Ctrl-C after this section.

## 5. MCP stdio tools/list and max_create

Create newline-delimited JSON-RPC requests outside the repository. Keep each object on one line:

```bash
cat > /tmp/max-agent-grok-e2e-evidence/mcp-requests.jsonl <<'JSON'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"grok-e2e","version":"1"}}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"max_create","arguments":{"goal":"Grok e2e MCP","acceptance_criteria":["worker_ok","output_equals:answer=mcp-ok"],"worker_kind":"mock","worker_payload":{"mode":"success","answer":"mcp-ok"},"idempotency_key":"grok-e2e-mcp-mock"}}}
JSON
uv run --no-sync python -m max_agent.cli.main mcp \
  < /tmp/max-agent-grok-e2e-evidence/mcp-requests.jsonl \
  > /tmp/max-agent-grok-e2e-evidence/mcp-responses.jsonl
```

The third response contains the normal MAX envelope as JSON text in `result.content[0].text`. Extract its `task_id`, then use the real worker and query commands:

```bash
uv run --no-sync python -m max_agent.cli.main --json run <MCP_TASK_ID>
uv run --no-sync python -m max_agent.cli.main --json status <MCP_TASK_ID>
uv run --no-sync python -m max_agent.cli.main --json events <MCP_TASK_ID>
```

- [ ] Initialize returned protocol `2024-11-05`.
- [ ] `tools/list` included the six MAX tools and `max_create` returned `isError: false`.
- [ ] The created task reached `succeeded` with verification and queryable events.
- [ ] No MCP polling loop or remote MCP service was introduced.

## 6. CLI worker stub, then operator-only vendor probes

First prove the isolated CLI-worker mechanics with both external execution and the real-worker requirement disabled:

```bash
unset MAX_AGENT_ALLOW_EXTERNAL_CLI MAX_AGENT_REQUIRE_REAL_WORKER
uv run --no-sync python -m max_agent.cli.main --json create \
  'Grok e2e CLI stub' -c worker_ok -w cli \
  --payload '{"vendor":"stub","workspace_root":"/tmp/max-agent-grok-e2e-workspaces"}' \
  -k grok-e2e-cli-stub --timeout 60
uv run --no-sync python -m max_agent.cli.main --json run <TASK_ID>
uv run --no-sync python -m max_agent.cli.main --json status <TASK_ID>
uv run --no-sync python -m max_agent.cli.main --json events <TASK_ID>
```

- [ ] Final status is `succeeded` and fixture pytest passed.
- [ ] Worker output says `vendor: stub`, `real_cli_invoked: false`, `require_real_worker: false`, and `external_cli_authorized: false`.
- [ ] The event journal and isolated `.max-agent/manifest.json` artifact are inspectable.

Only now may the **operator**, outside the Grok skill, enable external execution and forbid stub fallback:

```bash
export MAX_AGENT_ALLOW_EXTERNAL_CLI=1
export MAX_AGENT_REQUIRE_REAL_WORKER=1
command -v grok || true
command -v codex || true
command -v claude || true
```

Claude Code is supported under the same operator-only gate and uses `ANTHROPIC_API_KEY`. Authorized `auto` probes and selects vendors in this order: Grok → Codex → Claude. `MAX_AGENT_REQUIRE_REAL_WORKER=1` applies to every CLI task claimed by that worker; alternatively set `"require_real_worker":true` in one task's payload.

Repeat create/run/status/events with unique idempotency keys and one of these payloads at a time:

```json
{"vendor":"auto","workspace_root":"/tmp/max-agent-grok-e2e-workspaces","require_real_worker":true}
{"vendor":"grok","workspace_root":"/tmp/max-agent-grok-e2e-workspaces","require_real_worker":true}
{"vendor":"codex","workspace_root":"/tmp/max-agent-grok-e2e-workspaces","require_real_worker":true}
{"vendor":"claude","workspace_root":"/tmp/max-agent-grok-e2e-workspaces","require_real_worker":true}
```

Interpret the result honestly:

- If a binary is absent or its version probe fails, an explicit vendor task should finish `failed` with `adapter_failure: cli_unavailable`; record this classified skip and `cli_invoked: false`.
- With the requirement enabled, `auto` must not succeed through stub. If no real vendor is enabled it finishes `failed` with `adapter_failure: real_worker_required`, `cli_invoked: false`, and `real_cli_invoked: false`.
- If a real vendor is present, do not execute it until the operator has explicitly authorized its credentials, possible cost, network access, timeout, and non-production workspace. Real acceptance requires `real_cli_invoked: true`, successful verification, terminal `succeeded`, and artifacts. Verification/finalization enforce the same boundary for CLI reports.
- Never create a shell shim or other fake production CLI to make this check pass. A worktree and CLI sandbox flags are not OS or VM isolation.

The focused drill is `scripts/real_cli_e2e_drill.sh --real --vendor claude` (or `codex`, `grok`, `auto`). Without `--real` it runs only gate tests. Live mode writes `create/run/status/events` JSON plus `real-cli-result.json` under `/tmp/max-agent-real-cli-e2e.*`, returns non-zero for success without a real invocation, and labels unavailable vendors as skips.

After probing:

```bash
unset MAX_AGENT_ALLOW_EXTERNAL_CLI MAX_AGENT_REQUIRE_REAL_WORKER MAX_AGENT_API_TOKEN MAX_AGENT_WEBHOOK_SECRET XAI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY
```

## 7. Evidence and residual-risk checklist

Create `docs/operations/evidence/grok-e2e-YYYY-MM-DD.md` with secrets redacted. Include:

- [ ] Base commit, host/environment, database location, and exact real entry-point commands.
- [ ] Every task ID, create/run/final status, verification result, and event-type summary.
- [ ] CLI-worker `vendor`, probe reasons, `cli_invoked`, `real_cli_invoked`, `adapter_failure`, and artifact location.
- [ ] Explicit statement of whether a real external coding CLI ran. Stub-only success must say that external-CLI acceptance remains open.
- [ ] Quota/rate-limit/cost coverage, timeout and cancellation behavior, and crash/recovery coverage.
- [ ] Confirmation that no production credential, public API, deployment, merge, release, or retag was involved.
- [ ] Final `uv run --no-sync pytest -q` result and a link to the pull request check status.

Residual risks to carry forward until separately tested: real vendor availability and authentication, paid quota and latency, real subprocess timeout/cancellation, crash recovery with side-effect idempotency, SQLite single-writer behavior, and the fact that worktrees plus CLI sandbox flags are not VM isolation.
