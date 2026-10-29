# Known Limitations & Next Phase

## MVP 已知限制

1. **SQLite 单写者**：租约 TTL、fencing token 和崩溃恢复已落地。SQLite 用 `BEGIN IMMEDIATE` + CAS，不支持 `SKIP LOCKED`；多 worker 并发抢同一任务时后来者会等待锁然后失败，而不是并行跳过。PostgreSQL 才走 `FOR UPDATE SKIP LOCKED`。
2. **超时不能杀死线程**：超时后内核提交 `timed_out` 并不再采纳该次结果，但进程内线程可能仍在跑。副作用任务因此不会在无幂等键时自动重放。
3. **验收器较简**：规则型 criteria，无 LLM-as-judge / 截图对比。
4. **LocalCodeWorker 不是沙箱**：写入和 pytest 路径必须落在 `/tmp` 或 `/workspace`（含已存在的 `test_path`，解析符号链接之后）。子进程使用 argv、剥离凭据环境，并在超时后杀掉进程组。进程仍以宿主用户运行，没有网络命名空间、用户隔离或容器。路径白名单不能证明没有外部副作用。
5. **MCP 只是最小 facade**：stdio 提供 `max_create` / `max_status` / `max_events` / `max_cancel` / `max_approve` / `max_reject`。没有 resources、prompts，也没有托管远端 MCP。调用是一次性的，不轮询。
6. **无 Docker / Postgres**：本环境无 Docker，开发库为 SQLite。
7. **CLI Worker 默认不调用付费 CLI**：`worker_kind=cli` 在独立 git worktree 里运行。未设置 `MAX_AGENT_ALLOW_EXTERNAL_CLI=1`，或 `grok version` / `codex --version` 未成功时，不启动 Grok Build / Codex，改用仓库内 stub。授权后也只把 `XAI_API_KEY` 或 `OPENAI_API_KEY` 传给对应进程，不传数据库 URL、GitHub token 或宿主 HOME。Grok `--sandbox strict` 与 Codex `--sandbox workspace-write` 不是虚拟机边界；模型 API 仍可能出网。没有 Docker / 独立用户时，子进程仍是宿主用户。
8. **敏感操作**：`waiting_approval` 记录审批人、范围和过期时间。没有 Slack/邮件工单，也没有真实支付或发布。取消不能杀掉已经在跑的线程；不可逆工具要在动作前走内核边界。
9. **Alembic**：已配置；MVP 也可 `init_db()` 直接 `create_all`（测试与 CLI 默认路径）。
10. **成本**：只记录 worker 明确给出的 `measured` / `estimate`。没有报告时是 `unknown`，不会填 0，也不会调用付费 API 去补数。OpenTelemetry 导出仍未接线。

## 下一阶段建议

1. 若需要比 worktree + 进程组更强的隔离，再加容器或独立用户；当前 CLI Worker 只在显式授权且版本探测成功后调用 Grok Build 或 Codex。
2. 生产环境切换 PostgreSQL，让多 worker 使用 SKIP LOCKED 并行抢占。
3. 若需要完整 MCP（resources、远程托管、取消通知），在现有 stdio facade 上扩展，而不是另做一套任务库。
4. 丰富 Verification（pytest 报告解析、artifact hash、黄金文件）。
5. 把 AER outbox 接到 OpenTelemetry。本地 outbox 和目录导出已经可用。
6. 审批工单：Slack/邮件确认回调后再放行敏感任务。
7. 依赖图已有 `run_queue` 和环检测。还没有可视化，也没有跨进程的公平配额账本。
8. 可选：与 Grok Bot 例行（routines）对接，做周期任务。

## 需用户授权后才能继续的事项

- `git remote` / `git push` 到 GitHub 或其他远端
- 安装 Docker 或系统级服务
- 配置并调用付费 LLM / 编码 Agent API
- 任何生产部署、支付、对外发布、权限变更的真实副作用
