# MAX × WorkBuddy Lite × AER：目标全景架构与 P0 接入契约

> 状态：架构基线（目标态，不代表已部署或通过真机 / Live CLI 验收）
>
> 追踪：[#69 CodeBuddy Worker](https://github.com/Wike-CHI/max-agent/issues/69) · [#72 WorkBuddy Task UI](https://github.com/Wike-CHI/max-agent/issues/72) · [#70 AER Runtime](https://github.com/Wike-CHI/max-agent/issues/70) · [#71 Remote Workers](https://github.com/Wike-CHI/max-agent/issues/71)

## 产品边界

- **MAX = Control Plane**：任务 ID、Task DB、DAG、审批/策略、租约和 Fencing、调度与最终 Verification。MAX 是任务状态唯一写入者。
- **WorkBuddy Lite = Client / Workspace**：桌面/移动界面、对话、工具调用、Diff、任务执行可视化与用户操作。当前本地 CodeBuddy ACP 会话不等同于 MAX 持久任务。
- **CLI Runtime = Execution Plane**：CodeBuddy、Codex、Claude Code、Grok 及未来远程 Worker。执行器报告「执行结束」，不决定 MAX Task 是否成功。
- **AER = Experience / Verification Infrastructure**：Run、Event、Error、Recovery、Verification、Distillation、Retrieval、Usage 与经验效果。AER 保存来源与可复用经验，不控制 MAX 调度/权限。

## 逻辑架构

```text
 WorkBuddy Desktop / Mobile     Web Console        MCP / HTTP / Webhook
               \                  |                 /
                \----- MAX API / Task Entry ------/
                                |
 MAX Task DB --- TaskService ---+--- Policy / Approval
              Scheduler / Lease / Fencing
                  |          |
             WorkerRegistry  Verification Gate ---- MAX Journal
                  |                                   |
           CLI Worker Adapters                    AER Outbox (aer.v1)
           /     |      \                               |
     CodeBuddy  Codex   Claude                       AER Runtime Ingestor (P1)
        |                                               |
  isolated worktree                          Trace -> Verification -> Experience
        |                                               |
  code / test / artifacts                   Retrieval -> UsageSignal -> feedback
 ```

> 上图 AER Runtime Ingestor、WorkBuddy Task Entry 与 Remote Worker Adapter 仍为规划项。现阶段 MAX 的 AER 默认适配器为 NullAERAdapter，也支持 FileAERAdapter；文件导出不等于实际 AER 数据库写入。

## P0：CodeBuddy CLI Worker（#69）

### 设计约束

1. 本阶段将 **CodeBuddy 添加为显式 CLI vendor**（`worker_payload.vendor=codebuddy`）。现有 `auto` 默认优先级保持 Grok → Codex → Claude，不自动选取 CodeBuddy。
2. 复用 MAX `CliWorker`、独立 worktree、TaskService、lease/fencing、`run_bounded`、pytest / document acceptance、Journal 和 AER outbox；不新增 Task DB。
3. 执行必须设置 `MAX_AGENT_ALLOW_EXTERNAL_CLI=1`，并且 Worker 环境提供非空 `CODEBUDDY_API_KEY`；没有凭据不启动实际任务。启动 CLI 时使用隔离 HOME，不继承 WorkBuddy Desktop 的登录态。
4. 采用 WorkBuddy Lite 仓库已记录的非交互调用形态：`codebuddy -p "<prompt>" --output-format stream-json`。探测使用 `codebuddy --version`，将该兼容性标为**待真实 CLI 版本验证**。
5. 不加入 `--yolo`、绕过权限之类的高风险标志，不继承 WorkBuddy 的本地网关关闭鉴权配置。不能把 worktree 当成 OS/container sandbox。
6. 将 `CODEBUDDY_API_KEY` 列为 **仅 CodeBuddy vendor** 的环境变量允许列表；禁止转发 GitHub Token、MAX 数据库凭据或其他厂商密钥。日志必须脱敏。
7. CLI 输出的模型/Token/成本只有在具备明确上报字段时才记账；缺失记为 `unknown`，不可填零。
8. 默认行为不变：`stub` 依然可用于测试，但 `require_real_worker=true` 不允许其伪装成真实执行；CLI 退出成功也必须通过 MAX 独立 Verification。

### 当前能力边界

- **本 PR 的目标**：加入显式受控 CLI 启动、明确凭据边界和基于虚构可执行文件的端到端回归测试。
- **未覆盖**：CodeBuddy CLI 登录态借用、ACP `session/request_permission`、流式工具事件、会话恢复、Agent 子任务事件、与 WorkBuddy UI 的在线状态同步。
- **Live 验收尚需**：固定 CodeBuddy 版本，检查 `--version`、`-p` 与 `--output-format stream-json` 的真实输出/退出语义；确认工具授权按预期拒绝，且不会无提示执行超出 MAX 策略的副作用。
- **Go / No-Go**：在 Live Drill 和权限行为验收之前，这一适配器保持显式选择、默认不可自动路由，不宣称生产可用。

### P0 验收矩阵

| 场景 | 预期 |
|---|---|
| 未设置 `MAX_AGENT_ALLOW_EXTERNAL_CLI=1` | 不执行 CodeBuddy，不创建隔离工作空间 |
| 已授权但缺少 `CODEBUDDY_API_KEY` | 返回 `cli_auth_missing`，不进入真实任务 |
| `auto` 只有 CodeBuddy 可用 | 不自动选取 CodeBuddy |
| 显式 CodeBuddy + 受控假 CLI | 有 vendor/model/usage 记录；pytest 独立通过方可 success |
| 验收失败 | Task 不进入 succeeded，即使 CLI 自报完成 |
| 子进程超时/取消 | 保留原 MAX 超时/恢复语义 |
| AER 不可达 | Outbox 存失败/待重试，不回滚 Task |

## P1：AER Runtime 真正闭环（#70）

复用 `Journal -> aer_exports` 可靠投递，不复制调度数据库；新增版本化语义转换和 AER Ingestor sink，并为 `task_id / attempt_id / source_event_id / aer_run_id` 建立幂等关联。任务前检索、注入与回报效果需由 MAX 策略控制；不得把 Agent 自述 success 等同于 independent verified success。

## P2：跨机器执行（#71）

引入身份认证、Capability Registry、Worker claim/heartbeat/lease/fencing、上传证据和取消；复用现有 MAX 的 TaskService 与 Policy，而不是让远程 Worker 自己更新任务终态。仅在经过可重复的单机真实 CLI/ACP 验收后启动。

## WorkBuddy 客户端接入（#72）

保留现有本地 ACP Chat；新增 MAX Task 模式，客户端展示 `task_id`、Attempt、审批、Execution Timeline、Artifacts、Verification 和 AER Trace，断线重连以 MAX 数据库为真源。不得把客户端的简单确认弹窗当作 MAX 真实审批。

## 北极星端到端场景

`GitHub Issue -> MAX Task -> CodeBuddy Worker -> isolated edits / tests -> PR / CI evidence -> MAX Verification -> AER ingest -> verified Experience -> next retrieval`

以上每个箭头都要标注 implementation/test/live 状态。仓库已有组件可用不代表整条链已经完成；特别是 PR/CI 发布、AER 真实导入、跨进程断线恢复与远程调度都必须以实测证据认定。
