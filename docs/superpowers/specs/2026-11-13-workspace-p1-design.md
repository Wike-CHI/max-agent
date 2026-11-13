# Console P1 设计规格：Console 写权限、调度派发与授权边界

状态：书面设计，待用户审阅后另行编写实现计划。本文档只描述设计，不包含实现提交。
适用范围：`max-agent` 内核 HTTP API（`src/max_agent/api/`）、内核服务层（`src/max_agent/kernel/service.py`）与只读 Console 前端（`frontend/`）。
不适用范围：Cloudflare Worker 写代理、部署 workflow、MCP/CLI 的进程内权限。

---

## 背景与目标

### 现状

- Console 目前是纯只读界面。`frontend/src/model.ts` 只发出 `GET` 请求；访问令牌仅保存在 React `useState` 中，没有写入 localStorage、sessionStorage、IndexedDB 或 cookie。同域 FastAPI 提供 `/console/` 静态页面。`docs/console.md` 明确写了当前没有提交、审批、取消、执行能力。
- 详情页把 `dependencies` 以纯文本展示，没有任务图。
- 内核侧的写 API 其实已经存在，但 Console 与 Cloudflare 都没有使用：`POST /v1/tasks`、`POST /v1/tasks/graph`、`POST /v1/tasks/{id}/schedule`、`POST /v1/queue/run`、`POST /v1/tasks/{id}/cancel`、`POST /v1/tasks/{id}/approve`、`POST /v1/tasks/{id}/reject`。另有 `POST /v1/inbound/webhook`（签名认证，不是 Bearer）和 `POST /v1/aer/retry`（Bearer 全权）。
- 认证集中在 `src/max_agent/api/auth.py`：单一 `MAX_AGENT_API_TOKEN` 环境变量 + 单一 `MAX_AGENT_API_ACTOR` 主体。任何一个有效令牌对该主体名下的任务拥有全部读写能力，没有 capability scope 概念。
- 取消语义在 `src/max_agent/kernel/service.py` 的 `TaskService.cancel`：允许对任意非终态（含 `leased`/`running`/`verifying`）任务执行取消，转入 `cancelled`，写 `cancel_requested_at`，清空 `leased_by` 与 `lease_expires_at`；对终态任务抛 `ValueError`，HTTP 层表现为 409。
- Cloudflare Worker 只代理四个只读 GET（query/status/trace/events/query 语义），写路径返回 404/405；部署 workflow 仍是硬禁用状态。参见 `docs/cloudflare.md`。

### 目标

1. 给 Console 一条**受限的、可审计的**写路径：创建、派发、审批、拒绝、取消五类动作，各自由独立的、可撤销的凭据约束。
2. 引入真正的授权边界：多枚独立凭据映射到 scope 集合，服务器端强制执行，默认拒绝。
3. 保持现有所有权模型不变（`created_by == principal`），不在本 P1 做跨主体审批。
4. 让「派发」成为一个显式的、可见的后端动作，而不是隐含在创建路径里的自动状态跃迁。
5. 给所有状态变更加上乐观并发控制（`version`）与内容绑定（`content_hash`），避免对过期页面上的旧内容执行审批或取消。
6. 保持失败关闭：凭据配置错误时宁可 401/403，也不退化成全权。

### 非本 P1 目标（详细列表见「非目标」）

不引入 cron、不扩大 Goal 自主规划、不开放公网写、不部署 Cloudflare、不给生产外部 Agent 新增写权限、不把浏览器侧逻辑当成安全边界。

---

## 方案取舍与选择

用户已批准方案 A。本节写清三个方案的差异与取舍结论。

### 方案 A（已选择）：同一主体下的多枚独立 scope 凭据

在 `MAX_AGENT_API_ACTOR` 这一个账户下，注册多枚彼此独立的 Bearer secret，每枚映射到一个 scope 集合。所有 token 共享同一 owner（`created_by` / `MAX_AGENT_API_ACTOR`），任务的授权判定仍然是 `created_by == principal`。

- 优点：改动面小；与现有 #19 的所有权模型完全兼容，不需要新的跨主体授权路径；凭据可按用途签发与单独撤销；审计可按 `token_id` 区分发起者。
- 缺点：无法区分「谁提出的」和「谁批准的」这两个自然人/系统角色；同一主体下的凭据泄露不改变归属关系，只改变可执行动作集合。

### 方案 B（本 P1 不选）：创建者与审批者使用不同 principal

让创建凭据与审批凭据归属不同的 principal，从而用主体差异天然实现「制衡」。

- 不选的理由：现有 `authorize_task` 在 `created_by` 与调用 principal 不匹配时直接 403。实现方案 B 需要引入跨主体授权（委托、审批组、或组织级的「审批人可覆盖创建人」规则），这远超本 P1 的最小范围，且会改变现有所有权语义，风险面大。
- 保留价值：方案 B 仍是后续「双人复核」需求的方向，可在后续 P2 单独立项。

### 方案 C（否决）：继续单枚全权令牌 + 前端禁用按钮

保留单一 `MAX_AGENT_API_TOKEN`，靠 Console 前端把危险按钮置灰来限制操作。

- 否决的理由：禁用按钮只是 UX，任何人直接调用同域 HTTP 即可绕过；把高权限凭据放进浏览器持久存储还会扩大泄露面。它无法构成独立的授权边界，也无法满足「审批凭据与执行凭据分离」的要求。

### 选择结论

**采用方案 A**：多枚注册 Bearer secret，映射到 scope 集合，共享同一 owner。执行凭据与审批凭据在**凭据层面**分离（不同 token id、不同 scope 集合），而不是在主体层面分离；`dispatch` 与 `approve` 不得同时授予同一 token id，注册表加载时由服务器强制拒绝这种组合。

---

## 授权模型

### 两套命名，必须区分

本文档中「scope」与「tag 范围」是两个不同概念，全文严格区分：

- **API scope（API scope 集合）**：Bearer 凭据的能力集合，属于 HTTP 认证层。本文档讨论的 `read` / `create` / `dispatch` / `approve` / `cancel` / `execute_inline` / `ops` 都是 API scope。
- **审批 tag 范围（敏感 tag 范围）**：`AuthorizeRequest.scope` / `ApproveRequest.scope` 使用的敏感标签集合，取值如 `delete` / `deploy` / `payment` / `publish` / `permission` / `production` / `external_write`。它描述的是「这次审批覆盖哪些敏感动作」，与 API scope 无继承、无映射关系。

举例：持有 `approve` API scope 的 token 仍然必须显式提供覆盖全部敏感 tag 的审批 tag 范围，才可能审批一个带 `payment` tag 的任务；持有 `read` API scope 与不持有任何 API scope 是完全不同的两件事。

### Scope 集合定义

| API scope | 覆盖的能力 |
| --- | --- |
| `read` | 全部受保护的 GET 端点（tasks/query/status/trace/events/metrics/telemetry 等），以及只读查看 `awaiting_dispatch`、`content_hash`、`version` |
| `create` | `POST /v1/tasks`、`POST /v1/tasks/graph` |
| `dispatch` | `POST /v1/tasks/{id}/dispatch` |
| `approve` | `POST /v1/tasks/{id}/approve`、`POST /v1/tasks/{id}/reject` |
| `cancel` | `POST /v1/tasks/{id}/cancel` |
| `execute_inline` | `POST /v1/tasks/{id}/schedule`、`POST /v1/queue/run` |
| `ops` | `POST /v1/aer/retry` |

默认拒绝规则（服务器端强制）：

- 未在 scope 集合中列出的 scope，对相应写端点一律拒绝。
- 未知/未注册的令牌（含未登记进注册表的旧 `MAX_AGENT_API_TOKEN`）返回 **401**。
- 令牌有效且 enabled，但缺少所需 scope，返回 **403**。
- 有效但已 `enabled=false` 的令牌同样返回 401。

### 凭据注册表

- 注册表由环境变量 `MAX_AGENT_API_TOKEN_REGISTRY` 指向**本机 JSON 文件**（不入版本库，不进镜像）。
- 文件格式为 JSON 对象，键为 token id，值为记录；等价的列表形式亦可，但必须包含下列字段：

```json
{
  "console-read-2026-11": {
    "secret_sha256": "小写十六进制 sha256",
    "scopes": ["read"],
    "enabled": true
  }
}
```

- 每条记录的字段：
  - `id`：**非秘密**的稳定标识，用于审计与 `whoami`。若采用对象键形式，键即 `id`；若采用列表形式则显式给出 `id` 字段。
  - `secret_sha256`：对该 token 明文密钥的 UTF-8 字节做 SHA-256 后的**小写 hex**。文件中不存明文密钥。
  - `scopes`：API scope 字符串数组。
  - `enabled`：可选布尔，默认 `true`。
- 请求认证时，服务器计算 `sha256(token.encode("utf-8")).hexdigest()`，对注册表中每个 enabled 记录用 `hmac.compare_digest` 比对哈希。
- 审计日志与 journal **只记录 `token_id` 与 `principal`**，绝不记录明文密钥，也不记录 `secret_sha256`。
- 注册表在每次认证时读取当前文件内容（并提供缓存时必须能被文件变更失效，以保证撤销立即生效）；加载失败（文件不存在、JSON 非法、字段类型不对）时应用应当启动失败或让所有 HTTP 请求 401，不允许退化为「单枚全权令牌」模式。

### 注册表加载期的强制约束

服务器在加载注册表时**拒绝**以下组合，视为配置错误：

1. 同一 token id 同时拥有 `dispatch` 与 `approve`（自派自批）。
2. 同一 token id 同时拥有 `create` 与 `approve`（自建自批）。

实现要求：这两种组合在加载期就应当被拒绝（拒绝加载该条记录，或整体拒绝加载注册表并让服务以失败关闭的方式运行）；对应测试用例必须覆盖「注册表拒绝加载或拒绝该记录」两种可接受实现之一。

对于只建不批的自动化场景，操作员可以单独签发只含 `create` 的令牌。

### 所有权模型

任务授权仍然按 `created_by == principal` 判定，与现状一致。多枚 token 共享同一个 `MAX_AGENT_API_ACTOR`，因此在所有权上没有区别，区别只在 scope 集合与 `token_id` 上。跨主体审批不在本 P1 范围（见方案 B）。

### 旧 `MAX_AGENT_API_TOKEN` 的兼容规则（重要）

- **旧环境变量 `MAX_AGENT_API_TOKEN` 不再被 HTTP Bearer 自动视为全权凭据。**
- 如果旧令牌的 sha256 没有出现在注册表中，出示它一律返回 **401**。即使它此前拥有全部读写能力，也不再有任何 HTTP 权限。
- 如果操作员希望保留脚本的 HTTP 访问能力，必须在注册表中**新增一条记录**，显式给出 `id` 与**显式列出的 scopes**。可以写该秘密的 sha256。
- **禁止**给旧令牌默认写入全部 scope；**禁止**在文档或 Console 里把旧环境变量当作「只读控制台口令」。它过去是全权令牌，若仍按全权使用，就等于用旧令牌同时具备「写全权」与「当浏览口令用」，这正是本设计要消除的情况。
- 因此本设计的结论是：**旧全权 token 不能被当作 Console 的只读口令使用，同时仍然保留写全权**——它要么被显式收窄成某组 scope 的合法令牌，要么失去全部 HTTP 权限，不存在「只读地拿着写全权」这一中间态。
- Console 登录后必须先调用 `GET /v1/auth/whoami` 读取自身 scope。若该令牌同时具备任意写 scope（`create`/`dispatch`/`approve`/`cancel`）却被用作浏览会话，UI **必须明确显示其 scope**，不得把它宣传成只读凭据。推荐操作员单独签发 `read` 卡用于浏览。
- 没有「只读页面自动降低服务器权限」这种机制。权限只由服务器端按令牌注册信息判定。

### `GET /v1/auth/whoami`

- 任何已注册且 `enabled` 的令牌都可以调用，**不额外要求 `read`** scope。
- 返回体：`{"token_id": ..., "principal": ..., "scopes": [...]}`。
- **不返回**任何秘密、哈希或其他令牌信息。

### 进程内路径（CLI / MCP）

CLI 与 MCP 在本机进程内使用 `configured_principal()`，不经过 HTTP 认证层，因此不涉及 Bearer scope。本 P1 不改 MCP 的 scope 语义，CLI/MCP 的现有本机行为保持不变。HTTP 层一律以注册表为准。

### 匿名访问范围

- 匿名仅允许 `GET /health` 与 `GET /ready`。
- 其余 GET（tasks、query、status、trace、events、metrics 等）需要 `read` scope。
- `GET /v1/telemetry` 一并要求 `read` scope。若现状中该端点尚未挂认证依赖，P1 一并补上，避免在引入 scope 体系后留下新的匿名缺口。

---

## 写端点权限矩阵

下表覆盖**每一个**现有的写端点，以及本 P1 新增的端点。「所需 API scope」是必要条件，任务所有权校验（`created_by == principal`）在 scope 之外独立生效，两者都必须通过。

| 端点 | 方法 | 认证方式 | 所需 API scope | 额外校验 | 备注 |
| --- | --- | --- | --- | --- | --- |
| `/v1/tasks` | POST | Bearer | `create` | 所有权（创建后 owner = principal）；可选 `require_dispatch` | `require_dispatch=false`（默认）时保持现有自动 ready 行为 |
| `/v1/tasks/graph` | POST | Bearer | `create` | graph 级 `require_dispatch` 布尔，应用于该次请求创建的全部新节点；复用现有 `assert_acyclic` | 不新建规划器 |
| `/v1/tasks/{id}/dispatch` | POST | Bearer | `dispatch` | 所有权 + `expected_version` + `content_hash` | **本 P1 新增**；只改状态，不执行 worker |
| `/v1/tasks/{id}/approve` | POST | Bearer | `approve` | 所有权 + `expected_version` + `content_hash` + 审批 tag 范围覆盖任务全部敏感 tag | 幂等规则见后文 |
| `/v1/tasks/{id}/reject` | POST | Bearer | `approve` | 所有权 + `expected_version` + `content_hash` | 与 approve 共用 scope |
| `/v1/tasks/{id}/cancel` | POST | Bearer | `cancel` | 所有权 + `expected_version` + `content_hash` | 已终态返回 409（与现有代码一致） |
| `/v1/tasks/{id}/schedule` | POST | Bearer | `execute_inline` | 所有权 | **不发给 Console**；在调用进程内执行 `Scheduler.run_one`，不是入队 |
| `/v1/queue/run` | POST | Bearer | `execute_inline` | 无（队列级） | **不发给 Console**；在调用进程内执行 `run_queue`（claim + 跑 worker），不是入队 |
| `/v1/aer/retry` | POST | Bearer | `ops` | 所有者 | **不发给 Console** |
| `/v1/inbound/webhook` | POST | 签名（HMAC 等），非 Bearer | 不适用 | 沿用现有签名校验 | **不纳入 Bearer scope，不进入 Console**；创建语义维持当前自动 `_apply_initial_status`，`require_dispatch` 默认 false |
| `/v1/auth/whoami` | GET | Bearer | 无（任一 enabled 令牌） | — | 本 P1 新增，只读身份端点 |

### 读端点矩阵（摘要）

| 端点 | 方法 | 所需 API scope |
| --- | --- | --- |
| `/health`、`/ready` | GET | 匿名 |
| `/v1/telemetry` | GET | `read` |
| tasks / query / status / trace / events / metrics 等 | GET | `read` |

### Console 发放的令牌范围

Console 只可能签发 `read`、`create`、`dispatch`、`approve`、`cancel` 五种 scope 的组合。`execute_inline` 与 `ops` **不发给 Console**，只有操作员在注册表里显式授予时，本机脚本才能继续调用 `/schedule`、`/queue/run`、`/aer/retry`。

### 端点矩阵的实现约束

- scope 判定发生在路由依赖层（`Depends`），在进入业务处理之前返回 401/403。
- 所有权判定发生在服务层（`authorize_task`），scope 通过但所有权不匹配时返回 403。
- 版本/哈希不匹配返回 409，且**不产生任何状态变更**。

---

## 调度与 dispatch

### 现有行为与它为什么不适用于 Console

- 创建后 `_apply_initial_status`（`src/max_agent/kernel/service.py`）的处理：无依赖则 `pending` → `ready`；依赖未满足 → `blocked`；若有敏感 tag 再转入 `waiting_approval`（若先进入 `blocked`，则只记录 `last_error`，待依赖满足后再走审批流程）。
- `max-agent worker` 守护进程按现有领取循环认领 `ready` 任务。参见 `docs/operations/worker.md`。
- `POST /v1/tasks/{id}/schedule` 与 `POST /v1/queue/run` **在调用进程内执行** `Scheduler.run_one` / `run_queue`（claim 并直接跑 worker），它们不是入队操作。

### 明确不做的事

- **不把 `/schedule` 或 `/queue/run` 描述为入队**。它们是同步就地执行，本设计禁止把它们当作 Console 的默认调度路径，也禁止把它们当作 dispatch scope 的目标端点。
- 不引入 cron，不在浏览器里启动 worker。

### 设计：显式派发门

- 新增数据库列 `awaiting_dispatch`（布尔，默认 `false`），见「迁移、回滚与令牌轮换」。
- 创建请求可携带 `require_dispatch` 布尔：
  - `require_dispatch=false`（**默认**）：完全保持现有行为，任务照旧由 `_apply_initial_status` 自动进入 `ready`/`blocked`/`waiting_approval`。这是为了让现有 CLI、MCP、旧 HTTP 客户端与 `POST /v1/inbound/webhook` 的行为不发生变化。
  - `require_dispatch=true`：任务插入后保持 `pending`，并置 `awaiting_dispatch=true`。保守选择是**一律保持 `pending`**，把依赖判定与敏感 tag 判定推迟到派发时一次性复用 `_apply_initial_status`。
- `POST /v1/tasks/graph` 的 `require_dispatch` 是**图级布尔**，应用于该次请求创建的全部新节点。

### `POST /v1/tasks/{id}/dispatch` 的语义

1. 要求 `dispatch` API scope（否则 403）。
2. 校验任务所有权 `created_by == principal`（否则 403）。
3. 校验请求体中的 `expected_version` 与当前 `version` 一致、`content_hash` 与当前内容哈希一致；任一不匹配返回 **409**，且不执行任何状态变更。
4. 若 `awaiting_dispatch` 不为 `true`：
   - 若任务当前状态已是 `blocked` / `waiting_approval` / `ready`，且请求哈希与当前内容一致，则视为**幂等成功**，返回当前任务，不重复触发状态机；
   - 否则返回 **409**（状态不允许派发）。
5. 成功时：清除 `awaiting_dispatch`，调用现有 `_apply_initial_status`（依赖未满足 → `blocked`；有敏感 tag → `waiting_approval`；否则 → `ready`），递增 `version`，journal 记录含 `token_id`。
6. **该端点不得调用 `Scheduler.run_one` / `run_queue`，不得在 API 进程内执行 worker。** 它唯一的后端效果就是解除 `awaiting_dispatch` 并让任务进入既有状态机。

### 派发之后会发生什么

任务进入 `ready` 后，由**已经在本机运行的** `max-agent worker` 守护进程按现有领取循环认领并执行。操作员需要自行保证本机有常驻 worker。

Console UI 必须明确说明这一点：

- 派发按钮的语义是「解除 awaiting_dispatch 并进入既有状态机」，不是「立即执行」。
- 派发后 UI 显示「等待 worker 领取」状态，并提供指向现有 worker 操作说明的相对链接 `docs/operations/worker.md`。
- 浏览器不启动 worker，也不引入 cron。

### dispatch scope 的实际约束力

`dispatch` scope 对应的**真实后端动作**就是 `POST /v1/tasks/{id}/dispatch`：没有这张卡，请求无法解除 `awaiting_dispatch` 门，任务就停在 `pending`。只读查看（`read`）不足以改变这道门。没有这个门就不发放 `dispatch` 卡。

### 派发与审批的顺序关系

带敏感 tag 的任务在派发后进入 `waiting_approval`；批准后回到既有状态机继续推进。派发与审批分属不同 scope，不同 token id，因此形成「派发者不能审批」的结构性分离（主体仍是同一个 principal，但凭据不可互换）。

---

## 审批版本与内容哈希

### 内容哈希算法

`content_hash = sha256(utf-8(canonical_json))` 的小写 hex。

`canonical_json` 的规范：UTF-8 编码、对象键按字典序排序、无多余空白的 JSON。参与哈希的字段固定为以下 13 个：

`goal`、`acceptance_criteria`、`worker_kind`、`worker_payload`、`tags`、`dependencies`、`has_side_effects`、`issue_ref`、`priority`、`timeout_seconds`、`max_attempts`、`max_cost_units`、`allow_auto_retry_side_effects`。

这组字段足以绑定「被批准的是哪一份任务内容」。`version` **单独传递，不进入哈希**。

### 哈希计算与存储

- 哈希按字段即时计算，P1 **不新增 `content_hash` 数据库列**。
- `GET` 端点与 `TaskRead` 模型增加只读字段 `content_hash`；`awaiting_dispatch` 与 `version` 一并返回。
- 写入哈希时，缺失字段使用与序列化时相同的默认值填充，保证「读到的哈希」与「提交时计算的哈希」一致。

### 强制携带版本与哈希的端点

`POST /v1/tasks/{id}/approve`、`POST /v1/tasks/{id}/reject`、`POST /v1/tasks/{id}/cancel`、`POST /v1/tasks/{id}/dispatch` 的请求体**必须**包含 `expected_version` 与 `content_hash`。

- 任一不匹配 → **409**，且不得执行任何状态变更。
- approve 仍然必须覆盖任务的全部敏感 tag（审批 tag 范围），否则 **409**。
- 批准操作不得沿用过期页面上的旧哈希；UI 在用户确认前必须用当前详情重新拉取一次哈希。

### 幂等规则

| 操作 | 幂等条件 | 行为 |
| --- | --- | --- |
| approve | 同一 `token_id`/principal、同一 `content_hash`、审批 tag 范围已覆盖、决策仍为 granted、且未过期 | 返回当前任务，不递增 `version`，不重复写审批副作用 |
| reject | 已是 rejected/waiting_approval 且哈希一致 | 幂等返回当前任务 |
| cancel | 已是 `cancelled` | **保持现有 409**（与代码一致）。UI 在发现状态已是 cancelled 时视为「已完成」并刷新页面，不反复提交 |
| dispatch | `awaiting_dispatch` 已是 false，且当前状态为 blocked/waiting_approval/ready，哈希一致 | 幂等成功，返回当前任务 |
| create | 继续使用现有 `idempotency_key` | 现有语义不变 |

### 冲突处理

- CAS（`expected_version`）失败与非法状态继续返回 **409**。
- journal 记录冲突或成功事件，事件中包含 `token_id`（不含任何秘密或哈希）。

---

## 取消、租约与进程

### P1 的取消范围

P1 的 Console 取消**只调用现有 `POST /v1/tasks/{id}/cancel`**，加上 `cancel` scope、`expected_version` 与 `content_hash` 校验。**不新增杀进程协议，不接线 `cancel_file`。**

### 取消语义（按现有代码描述，不夸大）

1. 数据库状态立即变为 `cancelled`，并写入 `cancel_requested_at`，同时清空 `leased_by` 与 `lease_expires_at`。
2. 之后若有 finalize 到达，`finalize_in_session` 发现状态已不在 `leased`/`running`/`verifying` 之内，或 fencing / `leased_by` 不匹配，会执行 `_reject_fence` 并抛 `StaleFenceError`——**不会把已取消的任务改写成 `succeeded` 或 `failed`**。
3. 取消之后，标记为不可逆的工具在 `cancel_requested_at` 已设置或状态为 `cancelled` 时会被策略拒绝。
4. **正在运行的 CLI 子进程不保证立刻退出。** 它可能继续运行直到自身结束，或直到内核超时路径触发。超时路径会杀掉已登记的进程组，但那是**超时清理**，不是 cancel 的同步杀进程。
5. HTTP/API 的 cancel 路径当前**不会**写 `cancel_file`，也**不会**执行 `killpg`；`cancel_file` 只在 `worker_payload.cancel_file` 存在时由 sandbox 侧轮询并向进程组发 SIGTERM。P1 不改这一行为。

### 已知限制（已是结论）

「进程可能短暂残留」是本 P1 的**已知并接受**的限制，写入 `docs/console.md` 的相应说明与本规格。P1 明确不做进程级同步终止：未来若需要进程级终止，那是独立的设计项（涉及 `cancel_file` 接线与 sandbox 协议变更），不阻塞本 P1。

### UI 呈现要求

- 取消按钮必须二次确认，确认框展示 goal 摘要、内容哈希短前缀与目标状态。
- 取消返回 409 且任务已是 `cancelled` 时，UI 显示「任务已处于 cancelled」并刷新，而不是反复弹错误。

---

## Console UI 与凭据存储

### 凭据存储

- 浏览器**只把令牌放在内存**（沿用现有 `useState`）。
- **禁止** localStorage、sessionStorage、IndexedDB、cookie 持久化，也禁止把令牌放进 URL query、hash 或请求日志。
- 允许同时持有多枚内存令牌（例如「只读浏览卡」+「审批卡」），按操作类型自动选用对应的令牌。
- **不把全权卡当只读口令使用。** 若当前会话使用的令牌具备任意写 scope（`create`/`dispatch`/`approve`/`cancel`），UI 必须明确显示其 scope 列表，不得把它呈现为只读凭据。推荐操作员单独签发 `read` 卡用于浏览。
- 登录后第一步调用 `GET /v1/auth/whoami`，拿到 `token_id`、`principal` 与 `scopes` 后再启用相应按钮。

### 操作与确认

- 危险操作（创建、派发、审批、拒绝、取消）必须二次确认。确认框至少展示：
  - goal 摘要（截断显示）；
  - 内容哈希短前缀（用于核对「批准的正是这一份内容」）；
  - 目标状态（例如 pending → ready、waiting_approval → approved、running → cancelled）。
- **禁用按钮只是 UX。** 服务器端始终强制 scope 与所有权；前端的隐藏或置灰不构成任何安全保证。前端单测需要验证：即使用错误 scope 触发 UI 动作，仍然会收到服务器的 403。

### 网络路径

- 写操作只打**本机同域 FastAPI**（`/v1/*` 与 `/console/` 同源）。
- **不走 Cloudflare 代理**，不改 Worker 允许列表，不部署 Worker。参见 `docs/cloudflare.md`。

### 界面能力（P1）

- 任务列表 / 详情（只读已有能力 + 新增 `content_hash`、`awaiting_dispatch`、`version` 展示）。
- 创建任务（含 `require_dispatch` 开关）。
- 派发按钮（仅当 `dispatch` scope 存在且 `awaiting_dispatch=true` 时可用），派发后显示「等待 worker 领取」并链接 `docs/operations/worker.md`。
- 审批 / 拒绝（仅当 `approve` scope 存在，且审批 tag 范围覆盖任务敏感 tag 时可用；覆盖不全时按钮置灰并给出原因）。
- 取消（仅当 `cancel` scope 存在）。
- 多令牌切换与 scope 可视化。

---

## DAG 视图

- DAG 视图是**只读图**：节点 = 任务，边 = 依赖。
- 数据来源是已有的 `dependencies` 字段与任务状态机，**不新建规划器**，不自动拆分 Goal。
- 复用现有 `assert_acyclic` 与创建 graph 时的校验，不另造一套环检测。
- **P1 不新增 graph GET 端点**；前端从既有 query 结果在浏览器内构图。
- 创建图任务使用已有 `POST /v1/tasks/graph`，并置图级 `require_dispatch=true`，使其与「显式派发」模型一致；图内所有新节点同样处于 `awaiting_dispatch=true` 的 `pending` 状态，直到被显式派发。
- 详情页把现有的 `dependencies` 文本展示保留为兜底，同时提供图形化视图。

---

## 非目标

本 P1 明确不做以下事情：

1. **不引入 cron** 或任何浏览器端定时器来代替常驻 worker。
2. **不扩大 Goal 自主规划**：不引入自动拆解、自动拆 Goal 的规划器。
3. **不开放 Cloudflare 公网写代理**，也不部署 Cloudflare Worker，部署 workflow 保持硬禁用。
4. **不部署 Cloudflare**，不改 Worker 允许列表。
5. **不把 MCP/CLI 暴露到公网**，不改变其进程内 `configured_principal()` 语义。
6. **不给生产外部 Agent 新的写权限**；新 scope 只对本机注册表内的凭据生效。
7. **不实现跨主体审批**（方案 B）。
8. **不接线 `cancel_file`**，不实现取消时的同步进程终止。
9. **不新增 graph GET 端点**，不新增内容哈希列。
10. **不把浏览器验收、绕过本机回环防火墙等场景写入实现范围**。
11. **不把任务更新 / patch HTTP 接口纳入 P1**：任务无 HTTP 更新接口，`version` 仅用于内部 CAS 与本设计的乐观并发校验。

---

## 分阶段验收与测试清单

### 阶段 1（可独立验收，不含 UI）

交付内容：

- 令牌注册表加载与 scope 中间件（401/403 判定）。
- `GET /v1/auth/whoami`。
- 全部写端点的 scope 矩阵与测试。
- `require_dispatch` 请求参数、`awaiting_dispatch` 列、`POST /v1/tasks/{id}/dispatch`。
- 全部状态变更端点的 `expected_version` + `content_hash` 校验。

阶段 1 不改前端也可以合并验收。

阶段 1 pytest 清单（全部为本地测试，不调用付费模型）：

- [ ] 缺 token → **401**（全部受保护端点）。
- [ ] 只有 `read` 的令牌访问任一写端点 → **403**。
- [ ] `approve` 卡 → dispatch/create 端点 **403**。
- [ ] `dispatch` 卡 → approve/reject 端点 **403**。
- [ ] `create` 卡 → approve/dispatch 端点 **403**。
- [ ] `execute_inline` 与 `ops` 卡未发放给 Console；Console 令牌访问 `/schedule`、`/queue/run`、`/aer/retry` → **403**。
- [ ] `expected_version` 或 `content_hash` 不匹配 → **409**，且状态未变更。
- [ ] 敏感 tag 覆盖不全的 approve → **409**。
- [ ] 对终态任务 cancel → **409**。
- [ ] 幂等创建（复用现有 `idempotency_key`）。
- [ ] dispatch 幂等：`awaiting_dispatch` 已清除且状态为 ready/blocked/waiting_approval 时重复 dispatch → 幂等返回当前任务。
- [ ] journal 事件含 `token_id`，且**不含**明文密钥与 `secret_sha256`。
- [ ] 旧 `MAX_AGENT_API_TOKEN` 未写入注册表时，HTTP 请求 → **401**（即使该环境变量存在）。
- [ ] 旧令牌显式登记为只含 `read` 的记录后，只能读；登记为含写 scope 后，仍不能被当作只读口令（whoami 显示其 scope，UI 不得宣传为只读）。
- [ ] 同一 token id 同时含 `dispatch` + `approve` 的注册表 → **拒绝加载或拒绝该记录**；`create` + `approve` 同理。
- [ ] `require_dispatch=true` 创建的任务停留在 `pending` 且 `awaiting_dispatch=true`；`require_dispatch=false`（默认）保持现有自动 ready 行为。
- [ ] dispatch 后 `awaiting_dispatch=false`，状态按依赖/敏感 tag 进入 `blocked`/`waiting_approval`/`ready`。
- [ ] dispatch 端点**不**在 API 进程内执行 worker（断言无 worker 调用）。
- [ ] `POST /v1/inbound/webhook` 仍走签名认证，签名错误 → 拒绝；且不因 Bearer scope 体系而改变创建语义。
- [ ] `GET /v1/telemetry` 需要 `read`（若现状未挂认证，本阶段补上）。
- [ ] 现有 Cloudflare 测试仍断言写路径被禁止（404/405），未被本阶段放宽。

### 阶段 2（Console UI）

交付内容：创建 / 派发 / 审批 / 拒绝 / 取消界面、内存多令牌、危险操作确认框、DAG 只读视图。

阶段 2 前端测试清单：

- [ ] 无持久化存储：断言未调用 localStorage / sessionStorage / IndexedDB / document.cookie 写入。
- [ ] 令牌仅存在于组件内存状态。
- [ ] 登录后调用 whoami，并按 `scopes` 决定按钮可用性。
- [ ] 用错误 scope 触发 UI 动作 → 服务器仍返回 **403**（证明禁用按钮不是安全边界）。
- [ ] 危险操作弹出确认框，且确认框包含 goal 摘要、哈希短前缀、目标状态。
- [ ] 取消返回 409 且任务已是 `cancelled` 时，UI 刷新并显示「已完成」，不反复提交。
- [ ] DAG 视图由既有 query 结果在前端构图，环状依赖被拒绝（复用 `assert_acyclic`）。
- [ ] 写请求只发往同源 `/v1/*`，不出现在 Cloudflare Worker 代理路径上。

---

## 迁移、回滚与令牌轮换

### 数据库迁移

- Alembic 增加 `awaiting_dispatch` 布尔列，默认 `false`。
- 旧数据行不需要回填派发：迁移后全部为 `false`，即沿用现有自动状态机行为。

### 发布顺序

1. 先发布数据库迁移（新增列）。
2. 再发布应用版本。
3. **应用发布必须同时配置 `MAX_AGENT_API_TOKEN_REGISTRY`。** 若注册表缺失或不可解析，HTTP 读写请求将得到 401（以及注册表加载期拒绝的 403/启动失败）。这是**刻意失败关闭**，不是回归；运维需要在发布窗口内准备好注册表文件。

### 回滚

- 回退应用版本即可；`awaiting_dispatch` 列可以保留，旧代码会忽略它，不需要立刻 down migration。
- 回滚后旧版本使用单枚 `MAX_AGENT_API_TOKEN` 的全权语义；此时应同步清理注册表文件，避免残留配置造成歧义。

### 撤销令牌

- 在注册表中把该记录设为 `"enabled": false`，或直接删除该记录。
- 撤销**立即生效**：下一次请求返回 401，不要求重启服务。为此注册表必须在每次认证时读取当前内容（或提供等价的热重载），不得只在进程启动时快照一次。

### 轮换

- **先加新 id，再禁用旧 id**：在注册表中新增一条新 `id` 的记录（新 scope 按需），签发新密钥给使用方，确认使用方切换后，再把旧 id 设为 `enabled=false` 或删除。
- 轮换过程中允许新旧 id 并存于注册表；两者共享同一个 principal，因此所有权判定不受影响，审计可用 `token_id` 区分发起者。
- **密钥泄露视为立即禁用该 id**：不做缓慢轮换，直接 `enabled=false`，随后补发新 id。

### 审计与日志约束

- 审计事件与 journal 记录 `token_id`、`principal`、动作、结果（成功/冲突/失败）。
- 任何日志、journal、Console 页面、错误响应中都**不得**出现明文密钥或 `secret_sha256`。

---

## 明确选择（无 TBD）

以下选择已全部写死，实现时不得再摇摆：

1. **方案 A**：同一 `MAX_AGENT_API_ACTOR` 下的多枚独立 scope 凭据；方案 B 不在本 P1，方案 C 否决。
2. **Scope 集合**：`read`、`create`、`dispatch`、`approve`、`cancel` 发给 Console；`execute_inline`（仅 `/schedule`、`/queue/run`）与 `ops`（仅 `/aer/retry`）不发给 Console，仅操作员可显式授予。
3. **默认拒绝**：未列出的 scope 一律拒绝；未知令牌 401；有效但缺 scope 403。
4. **旧 `MAX_AGENT_API_TOKEN` 不再是 HTTP 全权凭据**：未登记进注册表则 401；要保留访问必须显式登记并显式列 scopes。
5. **旧全权 token 不能当 Console 只读口令却仍写全权**：不存在该状态；旧令牌要么被显式收窄为某个 scope 集合，要么完全失效。
6. **注册表**：环境变量 `MAX_AGENT_API_TOKEN_REGISTRY` 指向本机 JSON 文件；存 `id`、小写 hex `secret_sha256`、`scopes`、可选 `enabled`（默认 true）；用 `hmac.compare_digest` 比对；日志只记 `token_id` 与 principal。
7. **执行与审批凭据分离**：同一 token id 不得同时拥有 `dispatch` 与 `approve`，也不得同时拥有 `create` 与 `approve`；加载期强制拒绝。
8. **所有权不变**：`created_by == principal`；多 token 共享同一 actor；不做跨主体审批。
9. **whoami**：任一 enabled 令牌可调用，不额外要求 `read`，不返回秘密。
10. **匿名范围**：仅 `GET /health`、`GET /ready`；`GET /v1/telemetry` 需要 `read`（现状未挂认证则本 P1 补上）。
11. **webhook**：保持签名认证，不纳入 Bearer scope，不进入 Console，`require_dispatch` 默认 false，创建语义维持现有自动 `_apply_initial_status`。
12. **CLI/MCP**：继续走进程内 `TaskService` 与 `configured_principal()`，HTTP 层一律走注册表。
13. **调度模型**：新增 `awaiting_dispatch` 布尔列（默认 false）；`require_dispatch` 默认 false 以保持现有行为；为 true 时任务保持 `pending` 且 `awaiting_dispatch=true`，派发时一次性复用 `_apply_initial_status`。
14. **dispatch 端点**：需要 `dispatch` scope + 所有权 + `expected_version` + `content_hash`；成功时清 `awaiting_dispatch` 并调用 `_apply_initial_status`；**不调用 `Scheduler.run_one`/`run_queue`，不在 API 进程内执行 worker**；幂等规则按上文。
15. **`/schedule` 与 `/queue/run` 是就地执行而非入队**：不作为 Console 默认调度，不作为 dispatch scope 目标。
16. **worker 由操作员自行常驻**：UI 显示「等待 worker 领取」并链接 `docs/operations/worker.md`；不在浏览器启动 worker，不引入 cron。
17. **内容哈希**：`sha256(utf-8(canonical_json))` 小写 hex，canonical_json 为键排序、无多余空白的 UTF-8 JSON，字段固定为 `goal`、`acceptance_criteria`、`worker_kind`、`worker_payload`、`tags`、`dependencies`、`has_side_effects`、`issue_ref`、`priority`、`timeout_seconds`、`max_attempts`、`max_cost_units`、`allow_auto_retry_side_effects`；`version` 不进入哈希。
18. **不新增 content_hash 列**：哈希即时计算，`GET`/`TaskRead` 增加只读 `content_hash`，并同时返回 `awaiting_dispatch` 与 `version`。
19. **状态变更端点强制携带 `expected_version` 与 `content_hash`**：不匹配 409 且不产生状态变更；approve 仍需覆盖全部敏感 tag（审批 tag 范围，与 API scope 是两套概念）。
20. **幂等**：approve/reject/dispatch 按上文条件幂等；cancel 对已 `cancelled` 保持现有 409，由 UI 视为已完成并刷新；创建沿用 `idempotency_key`。
21. **取消**：只复用现有 `POST /cancel`，不新增杀进程协议，不接线 `cancel_file`；进程可能短暂残留是**已知并接受**的限制，写入文档与 `docs/console.md`，P1 不做进程级同步终止。
22. **凭据存储**：仅内存 `useState`，禁止 localStorage/sessionStorage/IndexedDB/cookie；可同时持多枚内存令牌按操作选用。
23. **危险操作二次确认**：展示 goal 摘要、哈希短前缀、目标状态；禁用按钮只是 UX，服务端强制 scope。
24. **写路径只走本机同域 FastAPI**：不改 Worker 允许列表，不部署 Cloudflare。
25. **DAG 只读**：节点=任务、边=依赖，复用 `assert_acyclic`，前端用既有 query 结果构图，P1 不新增 graph GET；建图用 `POST /tasks/graph` 且 `require_dispatch=true`。
26. **迁移**：Alembic 增加 `awaiting_dispatch` 布尔列（默认 false），旧行不需派发；应用发布必须同时配置注册表，否则 HTTP 401/403（刻意失败关闭）。
27. **回滚**：回退应用版本，列可保留；同步清理注册表以消除歧义。
28. **撤销**：注册表 `enabled=false` 或删除记录，下次请求立即 401。
29. **轮换**：先加新 id 再禁用旧 id；密钥泄露立即禁用该 id。
30. **非目标**已在上节列全，其中 cron、Goal 自主规划扩张、Cloudflare 公网写、Cloudflare 部署、MCP 上公网、生产外部 Agent 新写权限、跨主体审批、cancel_file 接线、graph GET 端点、content_hash 列、任务 patch 接口、浏览器端绕过均不在本 P1 范围。
