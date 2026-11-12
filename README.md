# MAX-Agent

Task-centric personal agent scheduler kernel.

**Task First, Agent as Worker.**

以任务为中心的个人 Agent 调度内核：任务由调用方创建并持久化，Worker 负责执行，可验证、可恢复、可审计。

## Requirements

- Python 3.12+（CI 在 3.12 / 3.13 上测试）
- `uv` recommended (or pip + venv)
- 前端构建需要 Node.js 24（仅构建期需要；运行时不依赖 Node 服务）

## Install

在仓库根目录执行：

```bash
uv sync --extra dev
# or: python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

## Initialize DB

数据库位置优先取 `MAX_AGENT_DATABASE_URL`，否则取 `MAX_AGENT_DB_PATH`（默认仓库内 `data/max_agent.db`）。

```bash
export MAX_AGENT_DB_PATH="$PWD/data/max_agent.db"
uv run max-agent init-db
# 已有库 / 备份恢复 / Postgres 运维走 Alembic 迁移路径：
uv run alembic upgrade head
```

`max-agent init-db` 与 `max-agent serve` 内部调用 `init_db()`（SQLAlchemy `create_all`），不会自动执行 Alembic 迁移；备份恢复与 Postgres 运维请显式执行 `uv run alembic upgrade head`（见 [docs/operations/postgres.md](docs/operations/postgres.md)）。

**HTTP API、MCP 与 worker 必须指向同一个库**（同一 `MAX_AGENT_DATABASE_URL` / `MAX_AGENT_DB_PATH`），否则控制台、Agent 查询和执行会看到不同的数据。

## CLI examples

```bash
# Create a mock task
uv run max-agent create "echo hello" \
  -c worker_ok -c 'output_equals:answer=hello' \
  -w mock --payload '{"mode":"success","answer":"hello"}' \
  -k demo-1

# Run one attempt manually, or leave execution to the long-running daemon
uv run max-agent run <TASK_ID>
uv run max-agent worker --concurrency 2 --poll-interval 1 --lease-ttl 600

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

CLI worker 默认在未授权时不调用外部付费 CLI，改用仓库内 stub。真实执行需要 `MAX_AGENT_ALLOW_EXTERNAL_CLI=1` 且厂商版本探测成功；验收类任务可设置 `worker_payload.require_real_worker=true` 或在 worker 进程设 `MAX_AGENT_REQUIRE_REAL_WORKER=1`，此时只有 `real_cli_invoked=true` 才能通过 verification。**stub 成功不等于真实 CLI 验收**，详见 [docs/operations/worker.md](docs/operations/worker.md)。

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

MCP 走 stdio，与 HTTP API 使用同一个数据库和同一个 `MAX_AGENT_API_ACTOR` 所有者。

## HTTP API

Set a strong API token before starting the server. Protected routes fail closed with 401 when it is missing or invalid.

Token 用命令生成随机值，不要写死固定密码，也不要提交到仓库：

```bash
export MAX_AGENT_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export MAX_AGENT_API_ACTOR=user  # optional; default: user
export MAX_AGENT_DATABASE_URL="sqlite:///$PWD/data/max_agent.db"
uv run alembic upgrade head       # existing DB: adds task ownership
uv run max-agent serve --host 127.0.0.1 --port 8080

curl -H "Authorization: Bearer $MAX_AGENT_API_TOKEN" http://127.0.0.1:8080/v1/tasks
# GET  /v1/health                 # anonymous liveness: process up, version
# GET  /v1/ready                  # anonymous readiness: SELECT 1
# GET  /v1/telemetry              # authenticated in-process spans and counters
# POST /v1/tasks
# GET  /v1/tasks                  # only tasks owned by the authenticated principal
# GET  /v1/tasks/query            # q / status / limit / cursor
# GET  /v1/tasks/{id}
# GET  /v1/tasks/{id}/status      # same envelope as `max-agent --json status`
# GET  /v1/tasks/{id}/events/query
# POST /v1/tasks/{id}/schedule
# POST /v1/tasks/{id}/cancel
# POST /v1/tasks/graph            # {"nodes":[{"key":"a","goal":"...","depends_on":[]}]}
# POST /v1/queue/run?limit=50     # only runs the principal's tasks
# POST /v1/tasks/{id}/approve     # {"note":"...","scope":[],"ttl_seconds":null}
# POST /v1/tasks/{id}/reject      # {"note":"..."}
# GET  /v1/tasks/{id}/events
# GET  /v1/tasks/{id}/trace       # issue -> worker -> verification -> pull request
# GET  /v1/metrics                # authenticated economics report
# POST /v1/aer/retry              # authenticated; does not run tasks
# POST /v1/inbound/webhook        # signed Routine/webhook create; returns task_id
```

Every task read/write, queue operation, telemetry, metrics, and AER retry request needs `Authorization: Bearer <token>`; only health/readiness and the independently signed webhook are exempt. The token principal comes from `MAX_AGENT_API_ACTOR` and is stored as `created_by`. Only that principal can read, schedule, cancel, approve, or reject the task. `actor` is optional in approval bodies for compatibility, but a value that differs from the principal returns 403; the recorded actor always comes from authentication. Migration assigns existing rows to `user`.

**Do not expose the HTTP API publicly without authentication.** Keep it on loopback or a private network until `MAX_AGENT_API_TOKEN` is configured. Remote access also requires TLS or a private tunnel so the bearer token is not sent in cleartext. This is a single-user token boundary, not multi-tenant IAM; OIDC and token rotation remain future work.

Webhook requests need `MAX_AGENT_WEBHOOK_SECRET` plus headers `X-Max-Timestamp`, `X-Max-Signature` (hex HMAC-SHA256 of `{timestamp}.{raw_body}`), and `X-Max-Delivery-Id`. Replays of the same delivery id return the original task. A new delivery with the same `idempotency_key` attaches to that task and does not start a second one. The handler does not run the worker. The task it creates is owned by the same `MAX_AGENT_API_ACTOR` principal as API-created tasks.

## Read-only MAX console

控制台由 PR #54 引入，已 squash 合入 master（`10a72ada`），不是待合并状态。

The console shows the same task database used by HTTP and MCP, at `/console/`.
It is **read-only**: it does not submit, approve, cancel, retry, or execute tasks.

在 `/console/` 可以查看：任务列表与查询（按 ID / 目标 / issue 引用过滤，带分页）、任务详情、事件 history、trace（尝试、证据、引用）、cost 标记、verification 结果。

```bash
# Node.js 24 is used in CI. Build once; no Node server is needed at runtime.
cd frontend
npm ci
npm test
npm run build
cd ..

# Use the same explicit DB URL for API, MCP, and the worker process.
export MAX_AGENT_DATABASE_URL="sqlite:///$PWD/data/max_agent.db"
export MAX_AGENT_API_ACTOR=user
# Set MAX_AGENT_API_TOKEN securely before starting; never commit it.
uv run alembic upgrade head
uv run max-agent serve --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080/console/` and enter the configured token. The page
holds it only in memory and forgets it when disconnected or reloaded. The token
still has its original API privileges; a read-only page is not a read-only credential.

没有前端构建产物时 API 仍可启动，`/console/` 返回 503；构建后重启 API 即可。

Agent callers can use `max_list_tasks`, `max_events_query`, and `max_trace` through
`max-agent mcp`, or the authenticated HTTP query endpoints. HTTP and MCP use the
same `MAX_AGENT_API_ACTOR` owner. The legacy local CLI remains an administrative
interface: `create` defaults to owner `user`, and reads are not owner-scoped.

See [console operations and query contracts](docs/console.md) for pagination,
identity, truthful execution/cost labels, packaging, and limitations.

### Cloudflare Workers 边缘部署（尚未公网部署）

Cloudflare 的测试与打包已经具备，但**尚未进行公网部署**：

- CI（[.github/workflows/ci.yml](.github/workflows/ci.yml)）的 `cloudflare-console` job 会安装锁定依赖、测试并打包只读 Worker（`npm test` / `build` / `test:runtime` / `check`），**不部署**。
- 部署 workflow（[.github/workflows/cloudflare-console.yml](.github/workflows/cloudflare-console.yml)）仍处于 hard-disabled 状态：只保留 `workflow_dispatch` 触发，deploy job 带字面量 `if: false`，且仍写死分支 `feat/53-cloudflare-console`。即使手动触发也无法真正部署。
- [docs/cloudflare.md](docs/cloudflare.md) 记录了启用前的一次性设置与启用提交步骤；在完成核对与授权之前不要宣称部署已生效。
- 浏览器 GUI 验收尚未完成，本文档不宣称 `/console/` 已通过浏览器验收。

For free Cloudflare hosting and Git-based console updates, see
[Cloudflare Workers deployment](docs/cloudflare.md). This hosts the UI and a
read-only proxy; the Python API and Task DB need a separate reachable backend.

## 费用与成本记账

供应商费用按 usage marker 与 ledger 记账，缺失不补零：

- usage marker：`measured`（实测）、`estimate`（估算）、`unknown`（未报告）。
- ledger（成本来源）：`grok_bot_quota`、`cli_quota`、`api_spend`。

CLI 报告里的 `usage.marker` 与金额会写入 `task_attempts`（cost_source / cost_marker / cost_amount / cost_currency / input_tokens / output_tokens）；CLI 未报告成本时这些字段保持 `unknown` 或为空，adapter 不会用 `0` 补齐。细节见 [docs/architecture.md](docs/architecture.md) 与 [docs/operations/worker.md](docs/operations/worker.md)。

## Goal 与 Agent Team（未完成）

- 本仓库当前**没有** `max_agent.goal` 包，也**没有**已发布的 `max-agent goal` 子命令；不要按已入库功能使用。
- Goal Workspace 与 Agent Team 尚未完成。
- 目前已实现的 Goal 能力仅限有限场景：CI 诊断（一次真实模型调用后离线重放）。这不是通用的自主端到端成功，也不代表多 Agent 编排已可用。

多 Agent 编排被明确列为当前阶段的非目标，见 [docs/ROADMAP.md](docs/ROADMAP.md) 与 [docs/limitations.md](docs/limitations.md)。

## Tests

```bash
uv run pytest -q

# 前端（构建期需要 Node.js 24）
cd frontend && npm ci && npm test && npm run build && cd ..
```

2026-11-12 合并前的归档结果：Python 192 passed / 1 skipped；前端 12 passed，并通过类型检查与生产构建。**这只是当时的归档快照，不是永久保证**，请以本地与 CI 的实际结果为准。

历史 MAX 任务中同时存在成功与失败记录；失败任务应如实保持为失败，不要改写成成功。

## Docs

- `docs/env-survey.md` — Phase 0 environment findings
- `docs/architecture.md` — architecture (bilingual notes)
- `docs/limitations.md` — known limits + next phase
- `docs/test-evidence.md` — captured pytest / demo output
- `docs/console.md` — console 查询契约、分页、身份与限制
- `docs/cloudflare.md` — Cloudflare Workers 边缘部署流程
- `docs/operations/README.md` — 运维 runbook 总览

## GitHub delivery workflow

- [Product roadmap](docs/ROADMAP.md): v0.1 MVP → Grok entry → durable execution → real workers → cost and orchestration.
- [Contribution / Issue–PR protocol](CONTRIBUTING.md): Issue → branch → PR → CI → squash merge → reviewed version tag.
- [CI workflow](.github/workflows/ci.yml): issue-link, Python 3.12/3.13 tests, CLI startup, package build, and the Cloudflare Worker test/bundle job.
- [Release workflow](.github/workflows/release.yml): manually tagged versions get tested, packaged, checksummed, and published as GitHub Releases. The provenance file is an unsigned SHA256 predicate.
- [Cloudflare console workflow](.github/workflows/cloudflare-console.yml): 当前 hard-disabled，无法实际部署。
- [Operations runbook](docs/operations/README.md): supply-chain audit, restore drill, staging smoke, production approval gate, health/SLO, and the branch-protection fallback. Production deploy remains manual.

## Safety

Remote changes, paid APIs, and external side effects require authorization. Protected routes fail closed without a token; keep the API on loopback or a private network, and use TLS or a private tunnel for remote access. Do not commit tokens, secrets, or machine-specific absolute paths. Branch protection may not be enforceable on the current plan, so human review and CI confirmation remain mandatory.
