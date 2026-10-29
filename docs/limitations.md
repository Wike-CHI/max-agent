# Known Limitations & Next Phase

## MVP 已知限制

1. **SQLite 单写者**：租约 TTL、fencing token 和崩溃恢复已落地。SQLite 用 `BEGIN IMMEDIATE` + CAS，不支持 `SKIP LOCKED`；多 worker 并发抢同一任务时后来者会等待锁然后失败，而不是并行跳过。PostgreSQL 才走 `FOR UPDATE SKIP LOCKED`。
2. **超时不能杀死线程**：超时后内核提交 `timed_out` 并不再采纳该次结果，但进程内线程可能仍在跑。副作用任务因此不会在无幂等键时自动重放。
3. **验收器较简**：规则型 criteria，无 LLM-as-judge / 截图对比。
4. **LocalCodeWorker**：仅允许写 `/workspace` 与 `/tmp`；不调用外部编码 Agent CLI。
5. **MCP 只是最小 facade**：stdio 提供 `max_create` / `max_status` / `max_events` / `max_cancel` / `max_approve`。没有 resources、prompts，也没有托管远端 MCP。调用是一次性的，不轮询。
6. **无 Docker / Postgres**：本环境无 Docker，开发库为 SQLite。
7. **无 CloudAgent CLI 集成**：宿主未暴露独立 `cloudagent` 可执行文件；编排层能力由 Grok Bot 父代理使用。
8. **敏感操作**：`approve` 把授权记在任务上。没有 Slack/邮件工单，也没有真实支付或发布。
9. **Alembic**：已配置；MVP 也可 `init_db()` 直接 `create_all`（测试与 CLI 默认路径）。
10. **成本预算**：`max_cost_units` 已建模，未计量真实 token/费用。

## 下一阶段建议

1. 接入真实编码 CLI（Codex / Claude Code / Cursor Cloud Agent）作为 Worker，经用户授权后启用。
2. 生产环境切换 PostgreSQL，让多 worker 使用 SKIP LOCKED 并行抢占。
3. 若需要完整 MCP（resources、远程托管、取消通知），在现有 stdio facade 上扩展，而不是另做一套任务库。
4. 丰富 Verification（pytest 报告解析、artifact hash、黄金文件）。
5. AER 导出（OpenTelemetry / 自定义 Agent Execution Record）。
6. 审批工单：Slack/邮件确认回调后再放行敏感任务。
7. 依赖图调度：批量 `run_ready()` 拓扑执行。
8. 可选：与 Grok Bot 例行（routines）对接，做周期任务。

## 需用户授权后才能继续的事项

- `git remote` / `git push` 到 GitHub 或其他远端
- 安装 Docker 或系统级服务
- 配置并调用付费 LLM / 编码 Agent API
- 任何生产部署、支付、对外发布、权限变更的真实副作用
