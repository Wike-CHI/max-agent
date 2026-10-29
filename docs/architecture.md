# MAX-Agent Architecture

**原则：Task First, Agent as Worker.**

MAX-Agent 是可独立部署的任务内核；Grok Bot 作为自然语言入口与云电脑环境，不在本仓库内复刻。

## 分层

```
┌─────────────────────────────────────────────┐
│  Adapters: CLI (Typer) / HTTP (FastAPI) /   │
│            (future) MCP                     │
├─────────────────────────────────────────────┤
│  Kernel                                      │
│  - TaskService   唯一状态写入路径            │
│  - Scheduler     单 Worker；预留租约/能力匹配 │
│  - StateMachine  合法状态迁移                │
│  - Verification  done ≠ success              │
│  - Journal       执行事件流；预留 AER 适配   │
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

`task_events` 表持久化 `created` / `status_changed` / `attempt_*` / `tool_call` / `verification` / `error` 等。  
`AERAdapter` Protocol + `NullAERAdapter` 预留导出。

## 人工审批策略

- 标签含 `delete|deploy|payment|publish|permission|production|external_write` → 需 `--confirm-side-effects`。
- `has_side_effects=true` 的任务自动重试需确认（或 budget 显式允许）。
- **不实现真实支付/部署**；仅策略钩子。

## 非目标（MVP）

- 不复刻 Grok Bot 聊天 / Computer Use / 多 Bot 团队
- 不依赖未文档化的宿主 SDK / 长驻服务
- 无 Web UI；无付费 API；无远程 push
