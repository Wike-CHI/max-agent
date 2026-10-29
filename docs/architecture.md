# MAX-Agent Architecture

**原则：Task First, Agent as Worker.**

MAX-Agent 是可独立部署的任务内核；Grok Bot 作为自然语言入口与云电脑环境，不在本仓库内复刻。

## 分层

```
┌─────────────────────────────────────────────┐
│  Adapters: CLI (Typer, --json) / HTTP       │
│            stdio MCP tools / signed webhook │
├─────────────────────────────────────────────┤
│  Kernel                                      │
│  - TaskService   唯一状态写入路径            │
│  - Scheduler     单 Worker；预留租约/能力匹配 │
│  - StateMachine  合法状态迁移                │
│  - Verification  done ≠ success              │
│  - Journal       执行事件流 + AER outbox      │
├─────────────────────────────────────────────┤
│  Workers (可替换)                            │
│  - MockWorker                                │
│  - LocalCodeWorker（本地写文件 + pytest）    │
├─────────────────────────────────────────────┤
│  Persistence: SQLAlchemy 2 + SQLite          │
│  (schema 兼容 PostgreSQL)                    │
└─────────────────────────────────────────────┘
```

## Task 模型

| 字段 | 说明 |
|------|------|
| id | UUID |
| goal | 目标描述 |
| acceptance_criteria | 验收条件列表（支持 `worker_ok` / `file_exists:` / `output_key:` / `output_equals:` / `regex:` / 子串） |
| priority | 越小越优先 |
| dependencies | 依赖任务 ID 列表 |
| status | 见状态机 |
| attempts / max_attempts | 尝试计数与上限 |
| timeout_seconds | 超时预算 |
| budget | max_cost_units、allow_auto_retry_side_effects |
| has_side_effects | 外部副作用标记；重试需确认 |
| worker_kind / worker_payload | Worker 类型与参数 |
| version | 乐观版本，claim / finalize 用 CAS 防并发覆盖 |
| fencing_token | 租约代次；过期抢占后旧 worker 的 finalize 会被拒绝 |
| lease / attempt | `leased_by` + `lease_expires_at`，以及 `task_attempts.lease_owner` |

## 状态机

```
pending → ready → leased → running → verifying → succeeded
                 ↘ blocked
running / verifying → retry_wait → ready (再跑)
任意非终态 → cancelled
不可恢复或耗尽尝试 → failed （可手动 reopen → ready）
```

非法迁移抛 `IllegalTransitionError`。

## Scheduler

- `run_one` 分成三段，并且每段各自提交：
  1. **claim+commit**：写入 `RUNNING`、租约 owner / `lease_expires_at` / `fencing_token` 和 `task_attempts` 行。
  2. **execute**：`worker.run()` 不持有 SQLAlchemy session。工具事件用单独的短事务写入。
  3. **finalize+commit**：对照验收条件；fencing token 不匹配则拒绝迟到结果。
- PostgreSQL claim 使用 `SELECT ... FOR UPDATE SKIP LOCKED`。SQLite 使用 `BEGIN IMMEDIATE` + version/status CAS，作为单 worker fallback，进程被杀掉后已提交的租约仍然在。
- 过期租约在下次 `run` / `recover` 时回收。无幂等键且未允许自动重试的副作用任务进入 `blocked`，不会自动再跑。
- `retry_wait.next_retry_at` 按尝试次数指数退避。显式 `run <id>` 可以立刻重试；自动捞取会等到到期。
- 预留 `LeaseManager`、`CapabilityMatcher` 类型。持久锁是数据库租约，不是内存对象。

## Verification

Worker 返回 `WorkerResult.ok` 只表示执行完成；内核对照 `acceptance_criteria` 验证后才可进入 `succeeded`。

失败分类写在任务和 attempt 上：

- `execution`：worker 报失败或抛异常
- `verification`：worker 自报成功，但验收未过
- `timeout`：超过 `timeout_seconds` 仍未返回（线程无法被杀死，只放弃这次 finalize）
- `crash`：租约过期仍未 finalize，按进程崩溃 / 失联处理

## Journal / AER

`task_events` 仍是执行日志。每条事件在同一事务里写入 `aer_exports` outbox，契约版本 `aer.v1`，`kind` 为 `task_run` / `attempt` / `event` / `verification`。

适配器在事务内投递。失败只把该行标成 `failed` 并记下 `last_error`，不回滚 Task、租约或验收。`max-agent aer-retry` 与 `POST /v1/aer/retry` 重投 `pending` / `failed`。默认 `NullAERAdapter` 只保留 outbox。设置 `MAX_AGENT_AER_DIR` 时，`FileAERAdapter` 按 `event_id` 覆盖写入 JSON，重试不会多出一份文件。

`GET /v1/tasks/{id}/trace` 用同一个 `task_id` 串起 issue、worker、verification、pull request。调用方没提供的环节标记为 `unknown`，不会从目标文本里猜。

## 成本与预算

Worker 报告 `usage.source`（`grok_bot_quota` / `cli_quota` / `api_spend`）和 `usage.marker`（`measured` / `estimate`）。没有 marker 的数字会被丢掉。运行时只有在 attempt 的起止时间都存在时记为 `measured`。

`max_cost_units == 0` 表示没有总上限。`budget.ceilings` 按账本单独设上限，`0` 是真实上限。已测花费超过上限时，下一次 claim 不会拿到租约，fencing token 不变。花费未知时不拦截，预算状态为 `unknown`。审批门仍先于预算检查。

指标 `verified_task_rate`、`cost_per_verified_task`、`retry_overhead` 在缺数时返回 `marker=unknown` 且 `value=null`。只有当已验证任务的每一次 attempt 都是 measured、并且都写明了账本时，才会把花费拆开；这时没被用到的账本合计是 0。否则该账本是 `unknown`，不会用 0 填上。

## Grok 入口

Grok Bot 只做一次性调用：`create` 立刻返回 `task_id`，之后按需 `status` / `events`。Worker 的 `run` 是单独进程，不由对话循环轮询。稳定字段见 `max_agent.entry.contract.envelope`。Skill 说明在 `docs/skills/grok-max-entry/SKILL.md`。stdio MCP 只实现 `initialize`、`tools/list`、`tools/call`。

Inbound webhook（`POST /v1/inbound/webhook`）校验 HMAC，用 `X-Max-Delivery-Id` 去重，并用 body 里的 `idempotency_key` 关联到已有任务。它只创建任务，不执行。

## 人工审批策略

- 标签含 `delete|deploy|payment|publish|permission|production|external_write` → 必须先 `approve`。`run --confirm-side-effects` 不能代替这次授权。
- `has_side_effects=true` 的重试仍需 `--confirm-side-effects`、budget 显式允许、幂等键，或已经 `approve`。
- **不实现真实支付/部署**；仅策略钩子。mock / local_code 演示不需要生产凭证。

## 非目标（MVP）

- 不复刻 Grok Bot 聊天 / Computer Use / 多 Bot 团队
- 不依赖未文档化的宿主 SDK / 长驻服务
- 无 Web UI；无付费 API；无远程 push
- 无 Task DAG / 公平队列 / `WAITING_APPROVAL`（那是 Issue #7）

## 回滚

`alembic downgrade c4a91e0b7d55` 会删除 `aer_exports` 以及任务上的 `issue_ref` / `budget_ceilings`、attempt 上的成本列。已导出到 `MAX_AGENT_AER_DIR` 的 JSON 是本地文件，删除该目录即可。这个变更不调用外部付费 API，也没有生产部署。
