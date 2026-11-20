# MAX console（只读查询 + P1 写路径）

## Scope

第一版（PR #54）是私有查询控制台：人机共享同一个 Task DB，没有任务执行与审批控件。
本 P1 在同一个 UI 上补齐**受作用域约束的写能力**：创建、显式派发、审批/拒绝、取消。
FastAPI 应用在同一 origin 上以 `/console/` 提供 React/Vite 构建产物。仍然没有新增数据库、
后台调度器、远程 MCP 服务或生产部署。

写路径不是「把页面变成自动化面板」：每一步都要受令牌作用域、职责分离和版本校验约束；
浏览器 GUI 目前**未验收**（见「GUI 验收状态与人工复核路径」），本文档不宣称 `/console/`
已通过浏览器视觉验收。

For the optional free edge-hosted console and Git-based updates, see
[Cloudflare deployment](cloudflare.md). That deployment serves the UI and a
read-only proxy; the Python API and Task DB still need their own reachable host.

The implementation is being prepared locally. Before publishing it, link a scoped
GitHub Issue and follow the repository's draft PR, CI, review, and release protocol.
This document is not authorization to push, merge, publish, or deploy.

## Run and package

1. Use Node.js 24 and run `npm ci && npm test && npm run build` in `frontend/`.
2. Set an explicit `MAX_AGENT_DATABASE_URL` (or absolute `MAX_AGENT_DB_PATH`) in
   API, MCP, and worker processes. All three must point at the same database.
3. Set `MAX_AGENT_API_ACTOR` to the same owner in API and MCP. The default is `user`.
4. Set a strong `MAX_AGENT_API_TOKEN` securely in the API process; do not commit it,
   add it to a URL, or include it in a frontend environment variable.
5. Run migrations before starting API/worker, then run
   `max-agent serve --host 127.0.0.1 --port 8080` and open
   `http://127.0.0.1:8080/console/`.
6. If execution is wanted, start `max-agent worker` separately using the existing
   [worker runbook](operations/worker.md). Viewing or refreshing the page never
   starts it.

The UI build lives in `src/max_agent/web/dist/`. After building it, run `uv build`
and `uv run python scripts/check_console_package.py`. CI and release verification
build and test the frontend before checking the wheel. A Python source install
without a UI build still runs the API and returns a helpful 503 at `/console/`.
Restart the API after creating a previously missing build.

For frontend development, run the API on port 8080, then `npm run dev` in
`frontend/`. Vite listens on loopback and proxies `/v1` to that API. The production
build is served by FastAPI and does not require a separate Node.js process.

## 令牌注册表 `MAX_AGENT_API_TOKEN_REGISTRY`

写路径需要**按作用域**区分的令牌，而不是一枚万能 bearer。令牌清单放在**本机**的一个
JSON 文件里，路径由环境变量 `MAX_AGENT_API_TOKEN_REGISTRY` 指定：

```json
{
  "tokens": [
    {
      "id": "console-read",
      "secret_sha256": "3f2b...（该令牌 secret 的 sha256 十六进制）",
      "scopes": ["read"]
    },
    {
      "id": "console-dispatch",
      "secret_sha256": "9a71...（该令牌 secret 的 sha256 十六进制）",
      "scopes": ["create", "dispatch", "cancel"]
    },
    {
      "id": "console-approve",
      "secret_sha256": "c40e...（该令牌 secret 的 sha256 十六进制）",
      "scopes": ["approve"]
    }
  ]
}
```

每条记录只有三个字段：`id`（人可读的令牌名）、`secret_sha256`（secret 的哈希，
注册表里**不存明文**）和 `scopes`（该令牌被授予的作用域列表）。文件本身是本机敏感
文件：不提交仓库、不进前端构建产物、不放进 URL、不贴进 issue 或日志。

要点：

- 明文 secret 只在生成时出现一次，之后浏览器只把它作为**内存令牌**持有；服务端靠
  `secret_sha256` 比对。
- 控制台支持同时持有**多枚内存令牌**（例如「只读卡」和「写卡」分持），用户在 UI
  里切换当前使用的令牌；每枚令牌的 scope 单独显示，界面必须让人一眼看出当前令牌
  能做什么、不能做什么。
- `GET /v1/auth/whoami` 返回当前令牌的 `id` 与 `scopes`。UI 在连接后立即拉一次
  `whoami`，据此决定哪些控件可用（例如只读卡不显示「审批」按钮），而不是靠前端
  硬编码猜权限。

## 令牌只存在浏览器内存

控制台把令牌保存在 JavaScript 内存里，**不落** localStorage、sessionStorage、
cookie，也不写进 URL 查询串或 hash。刷新页面、断开连接或关闭标签页后令牌即消失，
必须重新输入。这是控制台唯一的凭据保存方式，不存在「记住我」。

响应仍标记为 `no-store`，控制台保持同源内容策略，外链不发送页面 referrer。

## 作用域模型

发给 Console 的作用域只有这五个：

| scope | 含义 | 控制台动作 |
| --- | --- | --- |
| `read` | 查询 | 列表、过滤、分页、详情、事件、trace、status |
| `create` | 创建任务 | 新建任务（可带 `require_dispatch`） |
| `dispatch` | 显式派发 | 把 `awaiting_dispatch` 的任务交给本机 worker |
| `approve` | 审批 / 拒绝 | 审批或拒绝待审批任务 |
| `cancel` | 取消 | 取消任务 |

`execute_inline` 与 `ops` **不发给 Console**，也不授予控制台令牌。这两个作用域
服务于 `/schedule`、`/queue/run`、`/aer/retry` 这类「替我执行/重试/批量跑」的端点，
控制台不需要、也不应该能调用它们。写能力是「显式、可审计的单步动作」，不是
「一键把队列跑起来」。

### 职责分离：同一枚令牌不得同时持有 `dispatch+approve` 或 `create+approve`

同一枚令牌不得同时持有 `dispatch` 与 `approve`，也不得同时持有 `create` 与
`approve`。也就是说：**能派发的人不能自己审批，能创建的人不能自己审批**。

实践上的最小配置是分持的令牌：

- 只读卡：`scopes: ["read"]`，用于浏览和查询。
- 写卡（派发）：`scopes: ["create", "dispatch", "cancel"]`，用于创建、派发、取消。
- 审批卡：`scopes: ["approve"]`，只用于审批与拒绝。

也就是说，`approve` 永远待在**另一枚**令牌上；把 `approve` 和 `dispatch` 或
`create` 塞进同一枚令牌是违规配置，注册表应当拒绝这种组合。审批时在 UI 里切换
到审批卡。控制台在切换令牌后应重新拉取 `whoami`，确认框里也要显示**当前令牌
id**，避免误用。

## 创建：`require_dispatch=true`

控制台创建任务时显式带上 `require_dispatch=true`。效果是：任务创建后**停在**
`status = pending`，同时带上 `awaiting_dispatch=true` 标记，**不会**被自动交给
worker 执行。要真正开始执行，必须再走一次**显式派发**（`dispatch` 作用域）。

这条规则的作用是把「创建」和「执行」拆成两个可分别授权、分别记录的动作：
持 `create` 的用户可以创建任务并把它留在队列里等待别人派发，审批人也可以先审后派。

## 派发 ≠ 立即执行

点下「派发」只表示任务可以进入 worker 队列，**不代表任务已经跑完**，也不代表
此刻有 worker 在运行。要让任务真正被执行，本机必须常驻一个 worker 进程：

```bash
uv run max-agent worker --concurrency 2 --poll-interval 1 --lease-ttl 600
```

没有常驻 worker 时，派发后的任务会保持排队（仍是 `pending`，`awaiting_dispatch`
已清除但尚未起跑），直到 worker 起来领走它。worker 的运行要求、租约、真实 CLI
验收（`worker_ok` 之类的验收条件、`real_cli_invoked=true`）见
[worker 操作说明](operations/worker.md)，即 `docs/operations/worker.md`。
控制台**不会**为了演示而自动拉起 worker，也不会在页面刷新时顺带启动它。

## 审批 / 拒绝 / 取消：先拉版本与哈希，再提交

提交 approve、reject、cancel 之前，控制台会**重新拉取**该任务的
`expected_version` 与 `content_hash`，并在提交体里带上这两个值。服务端会比对：

- 任务在你打开确认框之后被改过（版本对不上）→ 拒绝本次提交，要求刷新后重试，
  避免把批准/取消打在过期的任务状态上。
- `content_hash` 对不上 → 同样拒绝。界面显示的是哈希短前缀，便于人工核对，
  不需要粘贴整串哈希。

若任务带有**敏感 tag**（sensitive tag），确认框必须显示该 tag，并要求操作者
**显式覆盖**（勾选/输入确认）之后才能提交；默认不自动放行，也不能由前端静默
预设成已确认。审批人、拒绝理由等记录照旧进入事件流，可审计。

## 取消的已知限制：残留与 `cancel_file`

取消（`cancel` 作用域）**只把任务标记为 cancelled**。它不保证正在运行的 CLI
子进程被立刻杀掉：

- 已经开始执行的 CLI 子进程可能**残留**一小段时间，继续占用资源，直到它自己
  退出；这段时间内任务状态已经是 cancelled。
- 本 P1 **不接线 `cancel_file`**：没有向 worker 进程写入取消标记文件，也没有
  任何「杀掉进程树」的实现，因此**不保证**取消后进程立即终止。
- 残留进程结束后仍可能写入事件与 attempt 记录；这些记录会保留，用于事后核对，
  不要因为「任务已取消」就忽略它们。

在 UI 上点「取消」之前，界面应提示这一限制（取消只改状态、不保证杀进程，可能有
残留进程），不要向用户暗示「取消 = 立刻停掉」。

## Cloudflare 边缘部署对写路径仍是只读

Cloudflare Worker 侧**只读**：边缘代理对写路径不做转发，写操作（创建、派发、
审批、拒绝、取消）必须直连本机/私网的 Python API。原因是写路径需要令牌作用域、
版本校验和同源会话，边缘只读代理既没有作用域判断，也不该承载审批动作。

- 边缘只读代理 + 同源 UI + 独立可达的 API/DB 的分工见 [docs/cloudflare.md](cloudflare.md)。
- 本次**不部署**任何东西：Cloudflare 部署 workflow 仍是 hard-disabled，CI 里的
  `cloudflare-console` job 只做测试与打包。文档不宣称边缘部署已生效。
- 顺带一提，Cloudflare 与「只读」这两个词在此处指同一件事的两面：边缘侧只读，
  写能力只在本机 API 上，二者不要混淆。

## GUI 验收状态与人工复核路径

**GUI 未验收**：本环境无法由自动化代理驱动浏览器做视觉验收，因此 `/console/` 的
写路径界面在本 P1 中处于「未验收」状态。不得绕过已知的回环/GUI 策略（例如不放宽
CORS/同源限制、不做端口转发），**不得开公网隧道**把本机 API 暴露到公网；也不要
用「截图看起来没问题」冒充人工复核结论。

合法的人工复核路径（由人在本机执行）：

1. **隔离数据库**：用单独的 sqlite 文件（例如 `data/console_p1_review.db`）设置
   `MAX_AGENT_DATABASE_URL`，先跑迁移。**不动真实用户任务库**，不往真实库里写
   复核用的任务。
2. **令牌注册表**：在本机准备 `MAX_AGENT_API_TOKEN_REGISTRY` 指向的 JSON 文件，
   按职责分离生成令牌并**分持**：**只读卡**（`read`）、**写卡**
   （`create`/`dispatch`/`cancel`）、**审批卡**（`approve`）。任何单枚令牌都不得
   同时拥有 `dispatch+approve` 或 `create+approve`。三枚令牌分别在本机输入到
   控制台，按需切换当前令牌。
3. **启动**：设置 `MAX_AGENT_API_ACTOR` 与 `MAX_AGENT_API_TOKEN`，运行
   `max-agent serve --host 127.0.0.1 --port 8080`，浏览器打开
   `http://127.0.0.1:8080/console/`（只连回环）。需要真实执行时另开终端运行
   `max-agent worker`。
4. **手工验证清单**（逐条记录结论与截图）：
   - 创建：新建任务带 `require_dispatch=true`，确认任务停在 `pending` 且
     `awaiting_dispatch=true`，没有被自动执行。
   - 显式派发：点击派发后确认任务进入可执行状态；若本机没有常驻 worker，确认它
     仍停在等待执行，而不是显示「已完成」。
   - 审批 / 拒绝：对一个待审批任务分别走审批与拒绝，确认事件流有记录，且敏感 tag
     必须显式覆盖才能提交。
   - 取消：取消一个运行中的任务，确认任务状态变为 cancelled，并确认界面/文档说明
     了「可能残留、不保证杀进程、不接线 `cancel_file`」这一限制。
   - DAG：提交带节点依赖的图（`POST /v1/tasks/graph` 形式的 `nodes` /
     `depends_on`），确认依赖顺序与节点状态展示正确。
   - 确认框内容：确认每类确认框都显示 **goal 摘要、哈希短前缀、目标状态**，
     并且在版本/哈希过期时拒绝提交、要求刷新。
   - 作用域：切换令牌后确认 `whoami` 反映的 scope 变化，且只读卡看不到写入口。
5. **边界**：不改防火墙规则、不开放公网入口、不做公网隧道、不部署 Cloudflare
   Worker、不触碰真实用户数据库。
6. **记录**：把人工结论写进对应 Issue/PR。在此之前，README 与本文档都不得宣称
   GUI 已通过验收。

## Authentication and identity

The console asks for the configured bearer token and keeps it in JavaScript
memory only. It does not save it in local/session storage, cookies, the URL, or a
frontend build. Reloading or disconnecting requires reconnecting. Responses are
marked `no-store`, the console has a same-origin content policy, and external
links do not send the page's referrer.

多枚令牌可以同时驻留内存，UI 按令牌分别显示 scope（见上文「令牌注册表」与
「作用域模型」）。持令牌本身**不等于**拥有全部写权限：服务端按注册表里该令牌的
`scopes` 判定，缺少作用域的写请求会被拒绝。

HTTP and MCP use the configured owner for task creation and queries. MCP rejects
access to another owner's task and rejects a spoofed approval actor. Local stdio
MCP is trusted local access, not a network authentication boundary. Existing rows
are not reassigned when `MAX_AGENT_API_ACTOR` changes. A different owner can
correctly see an empty list while another owner's tasks still exist.

Legacy CLI commands remain a trusted local administrative interface. CLI-created
tasks default to `user` and its reads are unscoped. Use MCP or HTTP for scoped
agent access, especially with a non-default owner. Do not change an actor merely
to bypass ownership checks.

## Shared read query contracts

- `GET /v1/tasks/query`: `q` (literal substring in task ID, goal, or issue reference;
  maximum 256 characters), optional `status`, `limit` (1–100, default 25), and
  optional opaque `cursor`. Returns `items`, `total`, and `next_cursor`.
- Task ordering is descending creation time, then ID. `total` means all matches
  for the current owner and filter, not the number on the current page. Cursors
  are bound to owner and filter and rejected if reused for a different query.
  They are positions, not database snapshots; concurrent updates may change the
  matching total. Refresh starts again from the newest page.
- `GET /v1/tasks/{id}/events/query`: `limit` (1–200, default 50) and optional
  `cursor`. The initial page contains the latest events in chronological order.
  `next_cursor` requests the next older page, which should be prepended. Returns
  `items`, `next_cursor`, and `has_more`.
- `GET /v1/tasks/{id}/status`: the stable machine envelope. `verification` is
  queried independently across the full journal, not inferred from a truncated
  event page.
- `GET /v1/tasks/{id}/trace`: recorded attempts, costs, verification evidence,
  references, and AER export state.

All require the existing bearer authentication and owner authorization. Invalid
query values/cursors produce 422, missing tasks 404, and other-owner tasks 403.
The old `/v1/tasks` and `/events` list contracts remain available. The legacy
events list still returns its first events up to the limit; use `/events/query`
for current progress and pagination. `/status.events` likewise remains a legacy
window; its separate `verification` is current.

MCP exposes `max_list_tasks` (task query fields), `max_events_query` (`task_id`,
`limit`, `cursor`), and `max_trace` (`task_id`) as read-only tools. Their successful
JSON results mirror the corresponding HTTP page or trace. Existing MCP commands
retain their envelopes. `max_status.verification` and CLI JSON verification also
read the actual latest verification. No query executes a worker.

## What the console can honestly show

- Task lifecycle and recorded lease information, not a real-time agent-presence
  system. A valid lease is not proof that a worker process is currently alive.
- 写按钮点击成功只说明**请求被受理**，不代表任务已执行完成；执行结果取决于本机
  是否常驻 `max-agent worker`。
- 取消后可能仍有残留的 CLI 子进程；「cancelled」是任务状态，不是进程终止证明。
- `mock`, `local_code`, and `cli` are distinct worker types. A CLI task alone is
  not proof of a real model call; worker output must record
  `real_cli_invoked=true`. Missing evidence stays unconfirmed. Stub success is
  not real-CLI acceptance. See [worker requirements](operations/worker.md).
- Cost markers remain `measured`, `estimate`, or `unknown`. Unknown cost is never
  replaced with zero, and unrelated ledgers/currencies are not fabricated into
  a monetary total.
- Issue/PR/artifact references are recorded evidence, not live GitHub checks.
  Local artifact paths are text, not downloadable browser URLs. Only HTTP(S)
  references can become external links.
- `/v1/telemetry` is process-local API telemetry, not worker fleet health. The
  console does not advertise agent-online counts from it.
- No percentage-progress estimate, automatic model execution, public sharing,
  production deployment, or remote approval is implied by this UI.

## Validation

Run the full Python suite, frontend tests/build, package asset check, and a browser
smoke test against an isolated fixture database. Cover connection errors,
authentication expiry, filters, pagination, long journals, latest verification,
detail navigation, disconnect/reconnect, and small screens. Fixture data must be
clearly distinguished from a real MAX deployment; never inject it into the user's
actual task database.

P1 写路径的自动化覆盖限于服务端口径（作用域判定、`require_dispatch` 落库为
`pending` + `awaiting_dispatch`、版本/哈希过期时拒绝提交、取消只改状态）。**GUI
部分在本环境无法由自动化代理驱动浏览器做视觉验收，因此仍属「未验收」**，必须按
上文「人工复核路径」由人在本机用隔离数据库手工走一遍：创建、显式派发、
审批/拒绝、取消、DAG 与确认框（goal 摘要、哈希短前缀、目标状态）。
