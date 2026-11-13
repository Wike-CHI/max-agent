# Console P1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为只读 Console 增加一条受限、可审计的写路径（创建 / 派发 / 审批 / 拒绝 / 取消），并把 HTTP 认证层从「单枚全权 `MAX_AGENT_API_TOKEN`」改造成「本机令牌注册表 + scope 集合 + 服务器端默认拒绝」，同时引入显式派发门（`require_dispatch` / `awaiting_dispatch`）与乐观并发控制（`expected_version` + `content_hash`），保持 CLI/MCP/webhook 现有行为不变。

**Architecture:** 三层改动。① 认证层：新增 `scopes.py`（scope 常量）与 `token_registry.py`（读 `MAX_AGENT_API_TOKEN_REGISTRY` 指向的本机 JSON，sha256 + `hmac.compare_digest` 比对，每次认证热读文件以保证撤销立即生效），`auth.py` 改为按 scope 的 `Depends` 依赖，未注册令牌 401、缺 scope 403。② 领域层：`kernel/service.py` 增加 `awaiting_dispatch` 派发门、13 字段 `content_hash` 计算、状态变更端点的 version/hash 校验与幂等规则；`kernel/dag.py` 的依赖唤醒路径不得让 `awaiting_dispatch` 任务自动就绪。③ 前端：`frontend/src/model.ts` 增加同源 `/v1/*` 写封装与 whoami，`frontend/src/main.tsx` 增加内存多令牌、scope 可视化、危险操作二次确认与只读 DAG 视图。所有写路径只走本机同域 FastAPI，不经 Cloudflare Worker。

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy, Alembic, pytest, React/Vite/Vitest（现有前端）

**Scope:** Issue #65（父 Epic #1）。相关仓库：Wike-CHI/max-agent。

---

## Global Constraints

以下约束在阶段 1、阶段 2 的**所有** Task 中都必须成立；任何 Task 若与之冲突，以本节为准。

- **G1 方案 A：同一 owner 多 token。** 所有注册 token 共享同一 owner（`MAX_AGENT_API_ACTOR`）。任务授权判定仍是 `created_by == principal`，本 P1 不做跨主体审批（方案 B 不实现、方案 C 否决）。
- **G2 scope 集合固定七个，服务器端默认拒绝。** `read` / `create` / `dispatch` / `approve` / `cancel` / `execute_inline` / `ops`。未列出的 scope 对相应端点一律拒绝；未注册令牌 401；有效但缺 scope 403；`enabled=false` 令牌 401。
- **G3 加载期禁止的 scope 组合。** 同一 token id 不得同时持有 `dispatch` + `approve`（自派自批），也不得同时持有 `create` + `approve`（自建自批）。违反即拒绝加载该注册表（失败关闭）。
- **G4 旧 `MAX_AGENT_API_TOKEN` 不再是全权凭据。** 其 sha256 未出现在注册表中时，出示它一律 **401**；要保留访问必须新增一条显式列出 scopes 的注册表记录。禁止给它默认写入全部 scope。
- **G5 派发门防偷跑。** `require_dispatch=true` 创建的任务必须保持 `pending` 且 `awaiting_dispatch=true`；依赖满足时**不得**因依赖变化自动 ready。`require_dispatch` 默认 `false`，保持 CLI/MCP/旧 HTTP 客户端/webhook 的现有自动 `_apply_initial_status` 行为。
- **G6 dispatch 只改状态。** `POST /v1/tasks/{id}/dispatch` **不得**调用 `Scheduler.run_one` / `run_queue`，不得在 API 进程内执行 worker；它唯一的后端效果是清 `awaiting_dispatch` 并复用 `_apply_initial_status`。
- **G7 version + content_hash 强校验。** `approve` / `reject` / `cancel` / `dispatch` 请求体必须携带 `expected_version` 与 `content_hash`；任一不匹配 → **409** 且**不产生任何状态变更**。哈希为 13 个固定字段的 SHA-256 小写 hex，`version` 不进哈希。
- **G8 取消不被 finalize 复活。** 取消只复用现有 `POST /cancel`，**不新增杀进程协议、不接线 `cancel_file`**；取消后到达的 finalize 走既有 `_reject_fence`，任务状态必须保持 `cancelled`，不得改写成 `succeeded` / `failed`。
- **G9 审计只记 `token_id`。** journal / 审计事件只记录 `token_id`、`principal`、动作、结果；**绝不**记录明文密钥、`secret_sha256` 或其它秘密。
- **G10 Console 仅内存凭据。** 令牌只放 React `useState`；禁止 localStorage / sessionStorage / IndexedDB / cookie / URL query / hash / 请求日志持久化。允许同时持有多枚内存令牌并按操作类型选用。
- **G11 范围红线。** 不引入 cron 或浏览器端定时器；不扩大 Goal 自主规划（不引入自动拆解/拆 Goal 规划器）；不开 Cloudflare 公网写代理、不改 Worker 允许列表、不部署 Worker；不把 MCP/CLI 暴露公网；不给生产外部 Agent 新增写权限；不实现跨主体审批；不新增 graph GET 端点；不新增 `content_hash` 数据库列；不实现任务 patch/update HTTP 接口。
- **G12 测试只用 fake worker。** 全部 pytest 与前端测试不得调用真实付费 CLI / 真实模型；执行类测试必须使用假 worker 或纯状态机断言。
- **G13 GUI 阻塞时不绕过。** 若前端验收被环境策略（回环防火墙、浏览器沙箱、CI 限制）阻塞，在 `docs/console.md` 与本 PR 描述中**明确标注「未验收」**，并给出复现步骤；禁止绕过策略、禁止把未跑通的 UI 写成已验收。
- **G14 提交纪律。** 阶段 1 与阶段 2 分开提交，每个 Task 独立 commit；commit message 使用 `feat` / `fix` / `test` / `docs` / `refactor` 前缀，正文末尾一律带 `Refs #65`。**测试命令由提交方在外部执行**，实现模型不执行 Bash。
- **G15 三层判定顺序。** scope 判定在路由依赖层（`Depends`，进入业务处理前返回 401/403）；所有权判定在服务层 `authorize_task`（不匹配 → 403）；version/hash 判定在服务层（不匹配 → 409）。三者独立生效。
- **G16 whoami 不要求 `read`。** 任一 enabled 令牌都可调用 `GET /v1/auth/whoami`，返回 `{"token_id","principal","scopes"}`，**不返回**任何秘密、哈希或其它令牌信息。
- **G17 匿名范围。** 匿名仅 `GET /health`、`GET /ready`；`GET /v1/telemetry` 映射为 `read` scope，不另开匿名缺口；其余受保护 GET 需 `read`。
- **G18 webhook 保持签名认证。** `POST /v1/inbound/webhook` 不纳入 Bearer scope、不进入 Console、创建语义维持自动 `_apply_initial_status`（`require_dispatch` 默认 false）。
- **G19 CLI/MCP 不变。** 进程内 `configured_principal()` 语义保持不变；HTTP 层一律以注册表为准。
- **G20 `/schedule` 与 `/queue/run` 是就地执行，不是入队。** 不得把它们描述为入队操作，不得作为 Console 的默认调度路径，也不得作为 `dispatch` scope 的目标端点；二者需 `execute_inline`，不发给 Console。
- **G21 危险操作二次确认。** 创建 / 派发 / 审批 / 拒绝 / 取消均需二次确认，确认框至少展示 goal 摘要、内容哈希短前缀、目标状态。禁用按钮只是 UX，服务端始终强制 scope 与所有权。
- **G22 撤销立即生效。** 注册表每次认证时热读当前文件内容；把记录设为 `enabled=false` 或删除后，下一次请求即 401，不要求重启。
- **G23 发布顺序与失败关闭。** 先发数据库迁移，再发应用版本，且应用发布**必须同时配置** `MAX_AGENT_API_TOKEN_REGISTRY`；注册表缺失/不可解析时 HTTP 读写请求 401（刻意失败关闭，不是回归）。
- **G24 计划文件唯一性。** 本次只写 `docs/superpowers/plans/2026-11-13-workspace-p1.md`；实现阶段的改动按「文件映射」执行，不得顺手重构无关大文件。

### 符号与夹具适配说明（一次性约定，全文适用）

本计划不复制仓库现有源码，因此对**已存在**的符号只固定「语义 + 调用形态 + 断言」，不重新发明签名。以下名字沿用仓库现状，实现时若实际名字不同，**只调整调用参数，不改断言、不改语义**：

| 语义 | 本计划使用的名字 | 约定 |
| --- | --- | --- |
| FastAPI 测试客户端 | `client` fixture | 沿用现有 `tests/conftest.py` 的 fixture |
| DB session | `db_session` fixture | 沿用现有 fixture |
| 服务层入口 | `service` fixture | 沿用现有 fixture |
| schema | `max_agent.models.schemas.TaskCreate` / `TaskRead` | 沿用现有名 |
| 服务类 | `max_agent.kernel.service.TaskService` | 沿用现有名 |
| 初始状态机 | `TaskService._apply_initial_status(task)` | 沿用现有名与语义 |
| 依赖唤醒入口 | `max_agent.kernel.dag.wake_satisfied(session, task_id)` | 沿用现有名与语义（只改「不自动就绪」的行为） |
| 收尾入口 | `max_agent.kernel.execution.finalize_in_session(...)` / `StaleFenceError` | 沿用现有名与语义（只锁住取消后行为） |
| worker 解析入口 | `max_agent.kernel.execution.resolve_worker(worker_kind)` | 用于在测试中替换为假 worker |
| journal 写入 | `TaskService` 现有 journal 写入函数 | 只要求新增 `token_id` / `principal` 关键字 |

---

## 文件映射

按现有结构扩展，不做无关重构；若 `auth.py` 变得过大，允许新增 `src/max_agent/api/auth_errors.py` 之类的辅助模块（仅在必要时）。

| 路径 | 动作 | 内容 |
| --- | --- | --- |
| `src/max_agent/api/scopes.py` | Create | API scope 常量、Console scope 集合、禁止组合、敏感 tag 常量 |
| `src/max_agent/api/token_registry.py` | Create | 注册表解析 / 校验 / sha256+`hmac.compare_digest` 比对 / 热读 / `AuthContext` |
| `src/max_agent/api/auth.py` | Modify | 去掉「仅 `MAX_AGENT_API_TOKEN` 即全权」；改为 `require_scope(scope)` 依赖工厂 + `require_any_token` |
| `src/max_agent/api/routes.py` | Modify | 各端点挂对应 scope；新增 `GET /v1/auth/whoami`、`POST /v1/tasks/{id}/dispatch`；四个写端点接收 `expected_version` / `content_hash` |
| `src/max_agent/models/schemas.py` | Modify | `TaskCreate` / `TaskGraphCreate` 增加 `require_dispatch: bool = False`；`TaskRead` 增加只读 `content_hash`、`awaiting_dispatch`、`version`；新增 `ApproveRequest` / `RejectRequest` / `CancelRequest` / `DispatchRequest` |
| `src/max_agent/db/models.py` | Modify | `tasks.awaiting_dispatch` 非空布尔列，默认 false |
| `alembic/versions/g0b2d4e15c33_awaiting_dispatch.py` | Create | `revision="g0b2d4e15c33"`，`down_revision="f19a0c3d8e21"` |
| `src/max_agent/kernel/service.py` | Modify | `require_dispatch` 创建门、`dispatch()`、13 字段哈希、version/hash 校验、幂等规则、journal `token_id` |
| `src/max_agent/kernel/dag.py` | Modify | 依赖满足时 `awaiting_dispatch` 任务保持 pending，不自动 ready |
| `src/max_agent/kernel/execution.py` | Modify（仅确认） | 确认 finalize 对已 `cancelled` 走 `_reject_fence`；不新增杀进程协议、不接线 `cancel_file` |
| `tests/conftest.py` | Modify | 注册表临时文件 fixture + `auth_header` + `make_task_body`；移除 `MAX_AGENT_API_TOKEN` 自动全权语义 |
| `tests/test_api_auth.py` | Modify | 401 矩阵、whoami、旧令牌 401、匿名范围 |
| `tests/test_token_scopes.py` | Create | 注册表加载/校验/热读/撤销 + 端点 scope 负面矩阵 |
| `tests/test_dispatch_gate.py` | Create | `require_dispatch` 创建门、dispatch 端点、幂等、依赖唤醒不偷跑 |
| `tests/test_content_hash_approval.py` | Create | 哈希算法、409、敏感 tag 覆盖、approve/reject 幂等 |
| `tests/test_cancel_finalize.py` | Create | cancel + version/hash、终态 409、取消后 finalize 不复活、journal 只记 `token_id` |
| `frontend/src/model.ts` | Modify | 同源 `/v1/*` 写封装、whoami、`ApiError`、类型 |
| `frontend/src/main.tsx` | Modify | 内存多令牌、scope 可视化、危险操作确认框、派发/审批/取消按钮 |
| `frontend/src/dag.ts` | Create | 浏览器内构图与环检测（只读） |
| `frontend/src/model.test.ts` | Create | 无持久化、同源路径、403 冒泡 |
| `frontend/src/dag.test.ts` | Create | 构图与环检测 |
| `docs/console.md` | Modify | 写能力说明、令牌发放建议、已知限制（进程残留）、未验收声明 |
| `README.md` | Modify | 一句能力说明（指向 `docs/console.md`） |

**哈希字段（固定 13 个，`version` 不入哈希）：**
`goal`、`acceptance_criteria`、`worker_kind`、`worker_payload`、`tags`、`dependencies`、`has_side_effects`、`issue_ref`、`priority`、`timeout_seconds`、`max_attempts`、`max_cost_units`、`allow_auto_retry_side_effects`。

---

## 阶段 1：服务端授权与派发门

阶段 1 可**独立验收**：只含服务端与 pytest，不改前端。完成后提交一次阶段 1 的整体验收说明。

### Task 1：令牌注册表、scope 常量、whoami 与旧令牌 401（含测试夹具迁移）

**Files:**
- Create: `src/max_agent/api/scopes.py`
- Create: `src/max_agent/api/token_registry.py`
- Modify: `src/max_agent/api/auth.py`
- Modify: `src/max_agent/api/routes.py`（新增 `GET /v1/auth/whoami`）
- Modify: `tests/conftest.py`
- Create: `tests/test_token_scopes.py`
- Modify: `tests/test_api_auth.py`

**Interfaces:**

```python
# src/max_agent/api/scopes.py
READ: str = "read"
CREATE: str = "create"
DISPATCH: str = "dispatch"
APPROVE: str = "approve"
CANCEL: str = "cancel"
EXECUTE_INLINE: str = "execute_inline"
OPS: str = "ops"

ALL_SCOPES: frozenset[str]
CONSOLE_SCOPES: frozenset[str]          # read/create/dispatch/approve/cancel
OPERATOR_ONLY_SCOPES: frozenset[str]     # execute_inline/ops
FORBIDDEN_SCOPE_COMBINATIONS: tuple[frozenset[str], ...]
SENSITIVE_TAGS: frozenset[str]           # delete/deploy/payment/publish/permission/production/external_write
```

```python
# src/max_agent/api/token_registry.py
REGISTRY_ENV_VAR: str   # "MAX_AGENT_API_TOKEN_REGISTRY"
ACTOR_ENV_VAR: str      # "MAX_AGENT_API_ACTOR"

class TokenRegistryError(RuntimeError): ...

@dataclass(frozen=True)
class TokenRecord:
    token_id: str
    scopes: frozenset[str]
    enabled: bool

@dataclass(frozen=True)
class AuthContext:
    principal: str
    token_id: str
    scopes: frozenset[str]

def registry_path() -> Path | None: ...
def configured_principal() -> str: ...
def load_registry(path: Path | None = None) -> dict[str, TokenRecord]: ...
def authenticate(token: str | None) -> AuthContext | None: ...
```

```python
# src/max_agent/api/auth.py
def require_scope(scope: str) -> Callable[..., AuthContext]: ...
def require_any_token(authorization: str | None = Header(default=None)) -> AuthContext: ...
require_principal = require_scope(READ)   # 兼容旧调用点，语义 = 需要 read
```

**Step 1.1 写失败测试（注册表单元测试 + 夹具迁移）**

先改 `tests/conftest.py`（这是后续所有测试的前提）：

```python
# tests/conftest.py（新增/替换片段，保留既有 fixture 不动）
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import pytest

REGISTRY_TOKENS: dict[str, dict[str, Any]] = {
    "test-read": {"secret": "test-read-secret", "scopes": ["read"]},
    "test-create": {"secret": "test-create-secret", "scopes": ["create"]},
    "test-approve": {"secret": "test-approve-secret", "scopes": ["approve"]},
    "test-dispatch": {"secret": "test-dispatch-secret", "scopes": ["dispatch"]},
    "test-cancel": {"secret": "test-cancel-secret", "scopes": ["cancel"]},
    "test-execute-inline": {"secret": "test-execute-inline-secret", "scopes": ["execute_inline"]},
    "test-ops": {"secret": "test-ops-secret", "scopes": ["ops"]},
}


@pytest.fixture(autouse=True)
def api_token_registry(tmp_path, monkeypatch):
    """把令牌注册表写到临时 JSON 文件，并通过环境变量指向它。

    规格禁止旧 MAX_AGENT_API_TOKEN 自动全权，因此这里显式删除该环境变量。
    """
    path = tmp_path / "token_registry.json"
    payload = {
        token_id: {
            "secret_sha256": hashlib.sha256(spec["secret"].encode("utf-8")).hexdigest(),
            "scopes": list(spec["scopes"]),
            "enabled": True,
        }
        for token_id, spec in REGISTRY_TOKENS.items()
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    monkeypatch.setenv("MAX_AGENT_API_ACTOR", "tester")
    monkeypatch.delenv("MAX_AGENT_API_TOKEN", raising=False)
    return path


@pytest.fixture
def auth_header():
    def _make(token_id: str) -> dict[str, str]:
        secret = REGISTRY_TOKENS[token_id]["secret"]
        return {"Authorization": f"Bearer {secret}"}

    return _make


@pytest.fixture
def make_task_body():
    def _make(**overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "goal": "demo goal",
            "acceptance_criteria": ["echo done"],
            "worker_kind": "echo",
            "worker_payload": {"cmd": ["echo", "hi"]},
            "tags": [],
            "dependencies": [],
            "has_side_effects": False,
            "issue_ref": "Wike-CHI/max-agent#65",
            "priority": 50,
            "timeout_seconds": 60,
            "max_attempts": 1,
            "max_cost_units": 0,
            "allow_auto_retry_side_effects": False,
        }
        body.update(overrides)
        return body

    return _make
```

再新建 `tests/test_token_scopes.py`（本 Task 只放注册表单元部分，HTTP 部分在 Task 2 追加）：

```python
from __future__ import annotations

import hashlib
import json

from max_agent.api.token_registry import TokenRegistryError, authenticate, load_registry


def _write(tmp_path, payload):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _record(secret: str, scopes: list[str], enabled: bool = True) -> dict:
    return {
        "secret_sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
        "scopes": scopes,
        "enabled": enabled,
    }


def test_authenticate_matches_sha256_of_secret(tmp_path, monkeypatch):
    path = _write(tmp_path, {"tok-a": _record("s3cret", ["read"])})
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    context = authenticate("s3cret")
    assert context is not None
    assert context.token_id == "tok-a"
    assert context.scopes == frozenset({"read"})
    assert context.principal == "tester"


def test_authenticate_returns_none_for_unknown_secret(tmp_path, monkeypatch):
    path = _write(tmp_path, {"tok-a": _record("s3cret", ["read"])})
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    assert authenticate("wrong") is None
    assert authenticate(None) is None


def test_disabled_token_is_rejected(tmp_path, monkeypatch):
    path = _write(tmp_path, {"tok-a": _record("s3cret", ["read"], enabled=False)})
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    assert authenticate("s3cret") is None


def test_registry_rejects_dispatch_plus_approve(tmp_path, monkeypatch):
    path = _write(tmp_path, {"tok-a": _record("s3cret", ["dispatch", "approve"])})
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    try:
        load_registry()
    except TokenRegistryError as exc:
        assert "approve" in str(exc)
    else:
        raise AssertionError("dispatch+approve 组合必须被拒绝")


def test_registry_rejects_create_plus_approve(tmp_path, monkeypatch):
    path = _write(tmp_path, {"tok-a": _record("s3cret", ["create", "approve"])})
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    try:
        load_registry()
    except TokenRegistryError as exc:
        assert "approve" in str(exc)
    else:
        raise AssertionError("create+approve 组合必须被拒绝")


def test_registry_rejects_unknown_scope(tmp_path, monkeypatch):
    path = _write(tmp_path, {"tok-a": _record("s3cret", ["read", "godmode"])})
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    try:
        load_registry()
    except TokenRegistryError:
        pass
    else:
        raise AssertionError("未知 scope 必须被拒绝")


def test_registry_rejects_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(tmp_path / "nope.json"))
    try:
        load_registry()
    except TokenRegistryError:
        pass
    else:
        raise AssertionError("注册表缺失必须报错而不是退化为单枚全权令牌")


def test_registry_is_hot_reloaded_so_revocation_is_immediate(tmp_path, monkeypatch):
    path = _write(tmp_path, {"tok-a": _record("s3cret", ["read"])})
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    assert authenticate("s3cret") is not None

    path.write_text(json.dumps({"tok-a": _record("s3cret", ["read"], enabled=False)}), encoding="utf-8")
    assert authenticate("s3cret") is None


def test_legacy_env_token_is_not_accepted(tmp_path, monkeypatch):
    path = _write(tmp_path, {"tok-a": _record("other", ["read"])})
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    monkeypatch.setenv("MAX_AGENT_API_TOKEN", "legacy-token")
    assert authenticate("legacy-token") is None
```

**Step 1.2 运行测试（提交方执行）**

```bash
uv run --no-sync pytest tests/test_token_scopes.py -v
```

预期 FAIL 原因：`ModuleNotFoundError: No module named 'max_agent.api.token_registry'`（模块尚未创建）。

**Step 1.3 最小实现**

`src/max_agent/api/scopes.py`：

```python
"""API scope 常量。

严格区分两套概念：
- API scope：Bearer 凭据的能力集合（本模块），属于 HTTP 认证层。
- 敏感 tag 范围：审批请求里覆盖的敏感标签（delete/deploy/payment/...），与 API scope 无映射关系。
"""

from __future__ import annotations

from typing import Final

READ: Final[str] = "read"
CREATE: Final[str] = "create"
DISPATCH: Final[str] = "dispatch"
APPROVE: Final[str] = "approve"
CANCEL: Final[str] = "cancel"
EXECUTE_INLINE: Final[str] = "execute_inline"
OPS: Final[str] = "ops"

ALL_SCOPES: Final[frozenset[str]] = frozenset(
    {READ, CREATE, DISPATCH, APPROVE, CANCEL, EXECUTE_INLINE, OPS}
)

# Console 只可能被发放这五个 scope。
CONSOLE_SCOPES: Final[frozenset[str]] = frozenset({READ, CREATE, DISPATCH, APPROVE, CANCEL})

# 只发给操作员本机脚本，不发给 Console。
OPERATOR_ONLY_SCOPES: Final[frozenset[str]] = frozenset({EXECUTE_INLINE, OPS})

# 加载期禁止的组合：同一 token id 不得自派自批、不得自建自批。
FORBIDDEN_SCOPE_COMBINATIONS: Final[tuple[frozenset[str], ...]] = (
    frozenset({DISPATCH, APPROVE}),
    frozenset({CREATE, APPROVE}),
)

# 敏感 tag 范围（审批用），与上面的 API scope 是两套概念。
SENSITIVE_TAGS: Final[frozenset[str]] = frozenset(
    {"delete", "deploy", "payment", "publish", "permission", "production", "external_write"}
)
```

`src/max_agent/api/token_registry.py`：

```python
"""本机令牌注册表。

注册表由环境变量 MAX_AGENT_API_TOKEN_REGISTRY 指向的 JSON 文件提供（不入版本库、不进镜像）。
每次认证都重新读取文件内容，保证撤销立即生效。
加载失败一律失败关闭（抛 TokenRegistryError），绝不退化为「单枚全权令牌」模式。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .scopes import ALL_SCOPES, FORBIDDEN_SCOPE_COMBINATIONS

REGISTRY_ENV_VAR = "MAX_AGENT_API_TOKEN_REGISTRY"
ACTOR_ENV_VAR = "MAX_AGENT_API_ACTOR"

_DEFAULT_PRINCIPAL = "operator"


class TokenRegistryError(RuntimeError):
    """注册表配置错误：文件缺失、JSON 非法、字段类型不对、scope 组合非法。"""


@dataclass(frozen=True)
class TokenRecord:
    token_id: str
    scopes: frozenset[str]
    enabled: bool


@dataclass(frozen=True)
class AuthContext:
    principal: str
    token_id: str
    scopes: frozenset[str]


def registry_path() -> Path | None:
    raw = os.environ.get(REGISTRY_ENV_VAR, "").strip()
    return Path(raw) if raw else None


def configured_principal() -> str:
    return os.environ.get(ACTOR_ENV_VAR, "").strip() or _DEFAULT_PRINCIPAL


def _is_sha256_hex(value: str) -> bool:
    if len(value) != 64:
        return False
    return all(ch in "0123456789abcdef" for ch in value)


def _parse_records(payload: Any) -> dict[str, TokenRecord]:
    if isinstance(payload, list):
        items: list[tuple[Any, Any]] = []
        for entry in payload:
            if not isinstance(entry, Mapping):
                raise TokenRegistryError("注册表列表项必须是对象")
            items.append((entry.get("id"), entry))
    elif isinstance(payload, Mapping):
        items = list(payload.items())
    else:
        raise TokenRegistryError("注册表顶层必须是对象或数组")

    records: dict[str, TokenRecord] = {}
    for token_id, body in items:
        if not isinstance(token_id, str) or not token_id.strip():
            raise TokenRegistryError("令牌 id 必须是非空字符串")
        if not isinstance(body, Mapping):
            raise TokenRegistryError(f"令牌 {token_id} 的记录必须是对象")

        secret_sha256 = body.get("secret_sha256")
        if not isinstance(secret_sha256, str) or not _is_sha256_hex(secret_sha256):
            raise TokenRegistryError(f"令牌 {token_id} 的 secret_sha256 必须是 64 位小写十六进制")

        scopes = body.get("scopes")
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise TokenRegistryError(f"令牌 {token_id} 的 scopes 必须是字符串数组")

        unknown = sorted(set(scopes) - ALL_SCOPES)
        if unknown:
            raise TokenRegistryError(f"令牌 {token_id} 含未知 scope: {unknown}")

        enabled = body.get("enabled", True)
        if not isinstance(enabled, bool):
            raise TokenRegistryError(f"令牌 {token_id} 的 enabled 必须是布尔")

        scope_set = frozenset(scopes)
        for combo in FORBIDDEN_SCOPE_COMBINATIONS:
            if combo <= scope_set:
                raise TokenRegistryError(
                    f"令牌 {token_id} 的 scope 组合非法：禁止同时持有 {sorted(combo)}"
                )

        records[token_id] = TokenRecord(
            token_id=token_id,
            scopes=scope_set,
            enabled=enabled,
        )
    return records


def load_registry(path: Path | None = None) -> dict[str, TokenRecord]:
    target = path if path is not None else registry_path()
    if target is None:
        raise TokenRegistryError(f"未设置 {REGISTRY_ENV_VAR}")
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise TokenRegistryError(f"无法读取令牌注册表 {target}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TokenRegistryError(f"令牌注册表 {target} 不是合法 JSON: {exc}") from exc
    return _parse_records(payload)


def authenticate(token: str | None) -> AuthContext | None:
    """按注册表当前内容校验 Bearer 明文。

    返回 None 表示：令牌为空、未注册、已禁用，或注册表不可用（失败关闭 → HTTP 401）。
    """
    if not token:
        return None
    try:
        records = load_registry()
    except TokenRegistryError:
        return None

    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    for record in records.values():
        if not record.enabled:
            continue
        if hmac.compare_digest(record.secret_sha256, digest):
            return AuthContext(
                principal=configured_principal(),
                token_id=record.token_id,
                scopes=record.scopes,
            )
    return None
```

`src/max_agent/api/auth.py`（替换认证部分，保留其余既有内容）：

```python
from __future__ import annotations

from typing import Callable

from fastapi import Header, HTTPException

from .scopes import READ
from .token_registry import AuthContext, authenticate


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def require_any_token(
    authorization: str | None = Header(default=None),
) -> AuthContext:
    """任一已注册且 enabled 的令牌即可通过；不要求 read，不返回任何秘密。"""
    context = authenticate(_extract_bearer(authorization))
    if context is None:
        raise HTTPException(status_code=401, detail="invalid or unregistered token")
    return context


def require_scope(scope: str) -> Callable[..., AuthContext]:
    """生成要求某个 API scope 的路由依赖。"""

    def dependency(authorization: str | None = Header(default=None)) -> AuthContext:
        context = authenticate(_extract_bearer(authorization))
        if context is None:
            raise HTTPException(status_code=401, detail="invalid or unregistered token")
        if scope not in context.scopes:
            raise HTTPException(status_code=403, detail=f"missing scope: {scope}")
        return context

    return dependency


# 兼容既有调用点：语义等价于「需要 read scope」。
require_principal = require_scope(READ)
```

`src/max_agent/api/routes.py` 新增端点（放在既有 `/v1` 路由旁）：

```python
from .auth import require_any_token, require_scope
from .scopes import READ
from .token_registry import AuthContext


@router.get("/v1/auth/whoami")
def whoami(context: AuthContext = Depends(require_any_token)) -> dict:
    """返回调用者身份与 scope 集合；不返回任何秘密或哈希。"""
    return {
        "token_id": context.token_id,
        "principal": context.principal,
        "scopes": sorted(context.scopes),
    }
```

同时把 `src/max_agent/api/auth.py` 中读取 `MAX_AGENT_API_TOKEN` 的旧逻辑**整体删除**（包括「该环境变量存在即全权」的分支）。

**Step 1.4 再跑同一命令**

```bash
uv run --no-sync pytest tests/test_token_scopes.py -v
```

预期 PASS（全部用例通过）。

**Step 1.5 迁移既有测试并补 401/whoami 测试**

在 `tests/` 中查找所有对旧令牌的引用并替换：

```bash
grep -rn "MAX_AGENT_API_TOKEN\|test-api-token" tests/
```

替换规则（**不是放宽规格，而是按 scope 精确化**）：

| 旧用法 | 新用法 |
| --- | --- |
| 读 GET 的测试 | `auth_header("test-read")` |
| 创建任务的测试 | `auth_header("test-create")` |
| 审批/拒绝的测试 | `auth_header("test-approve")` |
| 派发的测试 | `auth_header("test-dispatch")` |
| 取消的测试 | `auth_header("test-cancel")` |
| `/schedule`、`/queue/run` 的测试 | `auth_header("test-execute-inline")` |
| `/aer/retry` 的测试 | `auth_header("test-ops")` |

**同一枚 token 不得同时用于创建与审批**（G3）：原本「同一个 Bearer 既创建又审批」的测试必须拆成 `test-create` + `test-approve` 两枚令牌。

在 `tests/test_api_auth.py` 追加：

```python
def test_missing_token_returns_401(client):
    assert client.get("/v1/tasks").status_code == 401


def test_legacy_env_token_returns_401(client, monkeypatch):
    monkeypatch.setenv("MAX_AGENT_API_TOKEN", "legacy-token")
    resp = client.get("/v1/tasks", headers={"Authorization": "Bearer legacy-token"})
    assert resp.status_code == 401


def test_health_and_ready_are_anonymous(client):
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200


def test_whoami_needs_no_read_scope(client, auth_header):
    resp = client.get("/v1/auth/whoami", headers=auth_header("test-create"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_id"] == "test-create"
    assert body["principal"] == "tester"
    assert body["scopes"] == ["create"]
    assert "secret" not in body
    assert "secret_sha256" not in body


def test_whoami_rejects_unknown_token(client):
    assert client.get("/v1/auth/whoami", headers={"Authorization": "Bearer nope"}).status_code == 401
```

**Step 1.6 运行测试（提交方执行）**

```bash
uv run --no-sync pytest tests/test_token_scopes.py tests/test_api_auth.py -v
```

预期 PASS。若既有测试因令牌迁移而失败，按上表逐个替换。

**Step 1.7 提交**

```bash
git add src/max_agent/api/scopes.py src/max_agent/api/token_registry.py src/max_agent/api/auth.py src/max_agent/api/routes.py tests/conftest.py tests/test_token_scopes.py tests/test_api_auth.py
git commit -m "feat(api): 用令牌注册表与 scope 集合替换单枚全权 token" -m "Refs #65"
```

---

### Task 2：端点 scope 权限矩阵（负面矩阵全覆盖）

**Files:**
- Modify: `src/max_agent/api/routes.py`
- Modify: `tests/test_token_scopes.py`

**Interfaces:**

| 端点 | 方法 | 所需 scope |
| --- | --- | --- |
| `/v1/tasks` | POST | `create` |
| `/v1/tasks/graph` | POST | `create` |
| `/v1/tasks/{id}/dispatch` | POST | `dispatch`（Task 4 实现，本 Task 先挂依赖） |
| `/v1/tasks/{id}/approve` | POST | `approve` |
| `/v1/tasks/{id}/reject` | POST | `approve` |
| `/v1/tasks/{id}/cancel` | POST | `cancel` |
| `/v1/tasks/{id}/schedule` | POST | `execute_inline` |
| `/v1/queue/run` | POST | `execute_inline` |
| `/v1/aer/retry` | POST | `ops` |
| `/v1/inbound/webhook` | POST | 不适用（签名认证） |
| `/v1/auth/whoami` | GET | 无 |
| 其余受保护 GET（含 `/v1/telemetry`） | GET | `read` |

**Step 2.1 写失败测试**

追加到 `tests/test_token_scopes.py`：

```python
from __future__ import annotations

import hashlib
import json

import pytest

from max_agent.api.token_registry import TokenRegistryError, authenticate, load_registry  # noqa: F401


def test_read_token_cannot_create(client, auth_header, make_task_body):
    resp = client.post("/v1/tasks", json=make_task_body(), headers=auth_header("test-read"))
    assert resp.status_code == 403


def test_read_token_cannot_cancel(client, auth_header):
    resp = client.post(
        "/v1/tasks/does-not-exist/cancel",
        json={"expected_version": 1, "content_hash": "0" * 64},
        headers=auth_header("test-read"),
    )
    assert resp.status_code == 403


def test_create_token_cannot_approve(client, auth_header):
    resp = client.post(
        "/v1/tasks/does-not-exist/approve",
        json={"expected_version": 1, "content_hash": "0" * 64, "scope": ["payment"]},
        headers=auth_header("test-create"),
    )
    assert resp.status_code == 403


def test_create_token_cannot_dispatch(client, auth_header):
    resp = client.post(
        "/v1/tasks/does-not-exist/dispatch",
        json={"expected_version": 1, "content_hash": "0" * 64},
        headers=auth_header("test-create"),
    )
    assert resp.status_code == 403


def test_approve_token_cannot_create(client, auth_header, make_task_body):
    resp = client.post("/v1/tasks", json=make_task_body(), headers=auth_header("test-approve"))
    assert resp.status_code == 403


def test_approve_token_cannot_dispatch(client, auth_header):
    resp = client.post(
        "/v1/tasks/does-not-exist/dispatch",
        json={"expected_version": 1, "content_hash": "0" * 64},
        headers=auth_header("test-approve"),
    )
    assert resp.status_code == 403


def test_dispatch_token_cannot_approve(client, auth_header):
    resp = client.post(
        "/v1/tasks/does-not-exist/approve",
        json={"expected_version": 1, "content_hash": "0" * 64, "scope": ["payment"]},
        headers=auth_header("test-dispatch"),
    )
    assert resp.status_code == 403


def test_dispatch_token_cannot_create(client, auth_header, make_task_body):
    resp = client.post("/v1/tasks", json=make_task_body(), headers=auth_header("test-dispatch"))
    assert resp.status_code == 403


def test_console_tokens_cannot_schedule_or_run_queue(client, auth_header):
    for token_id in ("test-read", "test-create", "test-approve", "test-dispatch", "test-cancel"):
        resp = client.post(
            "/v1/tasks/does-not-exist/schedule",
            json={},
            headers=auth_header(token_id),
        )
        assert resp.status_code == 403, token_id
        resp = client.post("/v1/queue/run", json={}, headers=auth_header(token_id))
        assert resp.status_code == 403, token_id


def test_console_tokens_cannot_retry_aer(client, auth_header):
    for token_id in ("test-read", "test-create", "test-approve", "test-dispatch", "test-cancel"):
        resp = client.post("/v1/aer/retry", json={}, headers=auth_header(token_id))
        assert resp.status_code == 403, token_id


def test_operator_scoped_tokens_are_accepted(client, auth_header):
    resp = client.post(
        "/v1/tasks/does-not-exist/schedule",
        json={},
        headers=auth_header("test-execute-inline"),
    )
    assert resp.status_code != 403

    resp = client.post("/v1/aer/retry", json={}, headers=auth_header("test-ops"))
    assert resp.status_code != 403


def test_telemetry_requires_read_scope(client, auth_header):
    assert client.get("/v1/telemetry", headers=auth_header("test-read")).status_code == 200
    assert client.get("/v1/telemetry", headers=auth_header("test-create")).status_code == 403


def test_protected_get_requires_read(client, auth_header):
    assert client.get("/v1/tasks", headers=auth_header("test-read")).status_code == 200
    assert client.get("/v1/tasks", headers=auth_header("test-create")).status_code == 403


def test_webhook_still_requires_signature_not_bearer(client, auth_header):
    # 无签名 → 拒绝；带任意 Bearer 也不绕过签名
    assert client.post("/v1/inbound/webhook", json={"goal": "x"}).status_code in (401, 403)
    assert (
        client.post(
            "/v1/inbound/webhook",
            json={"goal": "x"},
            headers=auth_header("test-create"),
        ).status_code
        in (401, 403)
    )
```

**Step 2.2 运行测试（提交方执行）**

```bash
uv run --no-sync pytest tests/test_token_scopes.py -v
```

预期 FAIL 原因：写端点尚未挂 scope 依赖，`test_read_token_cannot_create` 等用例返回 200/404 而非 403。

**Step 2.3 最小实现**

在 `src/max_agent/api/routes.py` 中为每个端点挂上依赖（示例，逐一覆盖上表全部端点）：

```python
from .auth import require_any_token, require_principal, require_scope
from .scopes import APPROVE, CANCEL, CREATE, DISPATCH, EXECUTE_INLINE, OPS, READ


@router.post("/v1/tasks", response_model=TaskRead)
async def create_task(
    payload: TaskCreate,
    context: AuthContext = Depends(require_scope(CREATE)),
    service=Depends(get_service),
):
    ...

@router.post("/v1/tasks/graph", response_model=TaskRead)
async def create_graph(
    payload: TaskGraphCreate,
    context: AuthContext = Depends(require_scope(CREATE)),
    service=Depends(get_service),
):
    ...

@router.post("/v1/tasks/{task_id}/dispatch", response_model=TaskRead)
async def dispatch_task(
    task_id: str,
    payload: DispatchRequest,
    context: AuthContext = Depends(require_scope(DISPATCH)),
    service=Depends(get_service),
):
    ...

@router.post("/v1/tasks/{task_id}/approve", response_model=TaskRead)
async def approve_task(
    task_id: str,
    payload: ApproveRequest,
    context: AuthContext = Depends(require_scope(APPROVE)),
    service=Depends(get_service),
):
    ...

@router.post("/v1/tasks/{task_id}/reject", response_model=TaskRead)
async def reject_task(
    task_id: str,
    payload: RejectRequest,
    context: AuthContext = Depends(require_scope(APPROVE)),
    service=Depends(get_service),
):
    ...

@router.post("/v1/tasks/{task_id}/cancel", response_model=TaskRead)
async def cancel_task(
    task_id: str,
    payload: CancelRequest,
    context: AuthContext = Depends(require_scope(CANCEL)),
    service=Depends(get_service),
):
    ...

@router.post("/v1/tasks/{task_id}/schedule")
async def schedule_task(
    task_id: str,
    context: AuthContext = Depends(require_scope(EXECUTE_INLINE)),
    service=Depends(get_service),
):
    ...

@router.post("/v1/queue/run")
async def run_queue(
    context: AuthContext = Depends(require_scope(EXECUTE_INLINE)),
    service=Depends(get_service),
):
    ...

@router.post("/v1/aer/retry")
async def retry_aer(
    context: AuthContext = Depends(require_scope(OPS)),
    service=Depends(get_service),
):
    ...
```

所有受保护 GET 端点统一改为 `Depends(require_scope(READ))`（含 `/v1/telemetry`）。`/v1/inbound/webhook` 保持原签名依赖，不加 Bearer 依赖。

`DispatchRequest` / `RejectRequest` / `CancelRequest` / `ApproveRequest` 的 schema 在 Task 5 补齐；本 Task 先在 `src/max_agent/models/schemas.py` 建最小版本（Task 5 再补 version/hash 字段）：

```python
class DispatchRequest(BaseModel):
    expected_version: int
    content_hash: str
```

**Step 2.4 再跑同一命令**

```bash
uv run --no-sync pytest tests/test_token_scopes.py -v
```

预期 PASS（scope 负面矩阵全绿）。

**Step 2.5 提交**

```bash
git add src/max_agent/api/routes.py src/max_agent/models/schemas.py tests/test_token_scopes.py
git commit -m "feat(api): 为全部写端点挂载 scope 依赖与默认拒绝" -m "Refs #65"
```

---

### Task 3：`awaiting_dispatch` 列、Alembic 迁移与 schema

**Files:**
- Create: `alembic/versions/g0b2d4e15c33_awaiting_dispatch.py`
- Modify: `src/max_agent/db/models.py`
- Modify: `src/max_agent/models/schemas.py`
- Modify: `tests/test_dispatch_gate.py`（新建）

**Interfaces:**

```python
# src/max_agent/db/models.py —— Task ORM
awaiting_dispatch: Mapped[bool] = mapped_column(
    Boolean, nullable=False, default=False, server_default=text("false")
)
```

```python
# src/max_agent/models/schemas.py
class TaskCreate(BaseModel):
    ...  # 既有字段
    require_dispatch: bool = False

class TaskGraphCreate(BaseModel):
    ...  # 既有字段
    require_dispatch: bool = False

class TaskRead(BaseModel):
    ...  # 既有字段
    version: int
    awaiting_dispatch: bool
    content_hash: str
```

**Step 3.1 写失败测试**

新建 `tests/test_dispatch_gate.py`：

```python
from __future__ import annotations


def test_task_read_exposes_awaiting_dispatch_and_version(client, auth_header, make_task_body):
    resp = client.post("/v1/tasks", json=make_task_body(), headers=auth_header("test-create"))
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["version"], int)
    assert body["awaiting_dispatch"] is False
```

**Step 3.2 运行测试（提交方执行）**

```bash
uv run --no-sync pytest tests/test_dispatch_gate.py::test_task_read_exposes_awaiting_dispatch_and_version -v
```

预期 FAIL 原因：响应体里没有 `awaiting_dispatch` 字段（KeyError）。

**Step 3.3 最小实现**

`src/max_agent/db/models.py`：

```python
from sqlalchemy import Boolean, text  # 确保已导入

class Task(Base):
    ...
    awaiting_dispatch: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
```

`alembic/versions/g0b2d4e15c33_awaiting_dispatch.py`：

```python
"""add awaiting_dispatch column to tasks

Revision ID: g0b2d4e15c33
Revises: f19a0c3d8e21
Create Date: 2026-11-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "g0b2d4e15c33"
down_revision = "f19a0c3d8e21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "awaiting_dispatch",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("awaiting_dispatch")
```

`src/max_agent/models/schemas.py`：

```python
class TaskCreate(BaseModel):
    ...  # 既有字段保持不变
    require_dispatch: bool = False


class TaskGraphCreate(BaseModel):
    ...  # 既有字段保持不变
    require_dispatch: bool = False


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    ...  # 既有字段保持不变
    version: int
    awaiting_dispatch: bool
```

**Step 3.4 再跑同一命令**

```bash
uv run --no-sync pytest tests/test_dispatch_gate.py -v
```

预期 PASS。

迁移自检（提交方执行）：

```bash
uv run --no-sync alembic heads
uv run --no-sync alembic upgrade head
```

预期：`heads` 显示 `g0b2d4e15c33`，`upgrade head` 成功且旧数据行 `awaiting_dispatch` 全为 false。

**Step 3.5 提交**

```bash
git add alembic/versions/g0b2d4e15c33_awaiting_dispatch.py src/max_agent/db/models.py src/max_agent/models/schemas.py tests/test_dispatch_gate.py
git commit -m "feat(db): 新增 tasks.awaiting_dispatch 列与迁移" -m "Refs #65"
```

---

### Task 4：`require_dispatch` 创建门、dispatch 端点、依赖唤醒不偷跑、幂等 dispatch

**Files:**
- Modify: `src/max_agent/kernel/service.py`
- Modify: `src/max_agent/kernel/dag.py`
- Modify: `src/max_agent/api/routes.py`
- Modify: `src/max_agent/models/schemas.py`
- Modify: `tests/test_dispatch_gate.py`

**Interfaces:**

```python
# src/max_agent/kernel/service.py
async def create_task(
    self,
    *,
    principal: str,
    payload: TaskCreate,
    require_dispatch: bool = False,
    token_id: str | None = None,
) -> Task: ...

async def dispatch(
    self,
    task_id: str,
    *,
    principal: str | None = None,
    token_id: str | None = None,
    expected_version: int | None = None,
    content_hash: str | None = None,
) -> Task: ...
```

`dispatch()` 的判定顺序固定为：**所有权 → content_hash → 幂等 → expected_version → 状态机**。

**Step 4.1 写失败测试**

追加到 `tests/test_dispatch_gate.py`：

```python
from __future__ import annotations

import pytest


def _create(client, auth_header, make_task_body, **overrides):
    resp = client.post(
        "/v1/tasks",
        json=make_task_body(**overrides),
        headers=auth_header("test-create"),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_require_dispatch_task_stays_pending(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body, require_dispatch=True)
    assert task["status"] == "pending"
    assert task["awaiting_dispatch"] is True


def test_default_create_keeps_auto_ready(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body)
    assert task["awaiting_dispatch"] is False
    assert task["status"] == "ready"


def test_dispatch_clears_flag_and_enters_state_machine(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body, require_dispatch=True)
    resp = client.post(
        f"/v1/tasks/{task['id']}/dispatch",
        json={"expected_version": task["version"], "content_hash": task["content_hash"]},
        headers=auth_header("test-dispatch"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["awaiting_dispatch"] is False
    assert body["status"] == "ready"
    assert body["version"] == task["version"] + 1


def test_dispatch_is_idempotent(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body, require_dispatch=True)
    payload = {"expected_version": task["version"], "content_hash": task["content_hash"]}
    first = client.post(
        f"/v1/tasks/{task['id']}/dispatch", json=payload, headers=auth_header("test-dispatch")
    )
    assert first.status_code == 200
    second = client.post(
        f"/v1/tasks/{task['id']}/dispatch", json=payload, headers=auth_header("test-dispatch")
    )
    assert second.status_code == 200
    assert second.json()["version"] == first.json()["version"]
    assert second.json()["status"] == first.json()["status"]


def test_dispatch_with_wrong_hash_returns_409(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body, require_dispatch=True)
    resp = client.post(
        f"/v1/tasks/{task['id']}/dispatch",
        json={"expected_version": task["version"], "content_hash": "0" * 64},
        headers=auth_header("test-dispatch"),
    )
    assert resp.status_code == 409
    again = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read")).json()
    assert again["awaiting_dispatch"] is True
    assert again["status"] == "pending"
    assert again["version"] == task["version"]


def test_dispatch_with_stale_version_returns_409(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body, require_dispatch=True)
    resp = client.post(
        f"/v1/tasks/{task['id']}/dispatch",
        json={"expected_version": task["version"] + 99, "content_hash": task["content_hash"]},
        headers=auth_header("test-dispatch"),
    )
    assert resp.status_code == 409


def test_dispatch_does_not_execute_worker_in_api_process(client, auth_header, make_task_body, monkeypatch):
    import importlib

    scheduler_mod = importlib.import_module("max_agent.kernel.scheduler")
    calls: list[str] = []

    monkeypatch.setattr(
        scheduler_mod.Scheduler,
        "run_one",
        lambda *args, **kwargs: calls.append("run_one"),
        raising=False,
    )
    monkeypatch.setattr(
        scheduler_mod.Scheduler,
        "run_queue",
        lambda *args, **kwargs: calls.append("run_queue"),
        raising=False,
    )

    task = _create(client, auth_header, make_task_body, require_dispatch=True)
    resp = client.post(
        f"/v1/tasks/{task['id']}/dispatch",
        json={"expected_version": task["version"], "content_hash": task["content_hash"]},
        headers=auth_header("test-dispatch"),
    )
    assert resp.status_code == 200
    assert calls == []


def test_graph_require_dispatch_applies_to_all_new_nodes(client, auth_header, make_task_body):
    body = {
        "tasks": [
            make_task_body(goal="node a"),
            make_task_body(goal="node b"),
        ],
        "require_dispatch": True,
    }
    resp = client.post("/v1/tasks/graph", json=body, headers=auth_header("test-create"))
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    nodes = payload["tasks"] if isinstance(payload, dict) and "tasks" in payload else payload
    assert nodes
    for node in nodes:
        assert node["awaiting_dispatch"] is True
        assert node["status"] == "pending"


@pytest.mark.anyio
async def test_wake_satisfied_does_not_auto_ready_awaiting_dispatch(db_session, service):
    from max_agent.kernel.dag import wake_satisfied
    from max_agent.models.schemas import TaskCreate

    upstream = await service.create_task(
        principal="tester",
        payload=TaskCreate(goal="up", worker_kind="echo", worker_payload={"cmd": ["echo", "up"]}),
    )
    downstream = await service.create_task(
        principal="tester",
        payload=TaskCreate(
            goal="down",
            worker_kind="echo",
            worker_payload={"cmd": ["echo", "down"]},
            dependencies=[upstream.id],
        ),
        require_dispatch=True,
    )
    assert downstream.status == "pending"
    assert downstream.awaiting_dispatch is True

    upstream.status = "succeeded"
    await db_session.flush()
    await wake_satisfied(db_session, upstream.id)
    await db_session.refresh(downstream)

    assert downstream.awaiting_dispatch is True
    assert downstream.status == "pending"
```

**Step 4.2 运行测试（提交方执行）**

```bash
uv run --no-sync pytest tests/test_dispatch_gate.py -v
```

预期 FAIL 原因：`TaskCreate` 不接受 `require_dispatch`（422）、`dispatch` 端点返回 404、`awaiting_dispatch` 任务被自动置为 ready。

**Step 4.3 最小实现**

`src/max_agent/models/schemas.py`：

```python
class DispatchRequest(BaseModel):
    expected_version: int
    content_hash: str
```

`src/max_agent/kernel/service.py`：

```python
# 创建路径：require_dispatch 时保持 pending 且 awaiting_dispatch=True，
# 绝不能调用会把它变成 ready 的 _apply_initial_status。
async def create_task(
    self,
    *,
    principal: str,
    payload: TaskCreate,
    require_dispatch: bool = False,
    token_id: str | None = None,
) -> Task:
    task = Task(
        goal=payload.goal,
        acceptance_criteria=list(payload.acceptance_criteria or []),
        worker_kind=payload.worker_kind,
        worker_payload=dict(payload.worker_payload or {}),
        tags=list(payload.tags or []),
        dependencies=list(payload.dependencies or []),
        has_side_effects=bool(payload.has_side_effects),
        issue_ref=payload.issue_ref,
        priority=payload.priority,
        timeout_seconds=payload.timeout_seconds,
        max_attempts=payload.max_attempts,
        max_cost_units=payload.max_cost_units,
        allow_auto_retry_side_effects=bool(payload.allow_auto_retry_side_effects),
        created_by=principal,
        awaiting_dispatch=bool(require_dispatch),
        status="pending",
    )
    self._db.add(task)
    await self._db.flush()

    if not require_dispatch:
        # 保持现有行为：自动进入 ready/blocked/waiting_approval
        self._apply_initial_status(task)
    else:
        # 派发门：保持 pending，推迟依赖判定与敏感 tag 判定
        task.status = "pending"

    await self._db.flush()
    return task


async def dispatch(
    self,
    task_id: str,
    *,
    principal: str | None = None,
    token_id: str | None = None,
    expected_version: int | None = None,
    content_hash: str | None = None,
) -> Task:
    task = await self._get_task(task_id)
    self.authorize_task(task, principal)

    current_hash = content_hash_of(task)
    if content_hash is not None and content_hash != current_hash:
        raise TaskStateConflict("content_hash 不匹配")

    if not task.awaiting_dispatch:
        # 幂等：已派发且状态允许，返回当前任务，不重复触发状态机
        if task.status in {"blocked", "waiting_approval", "ready"}:
            return task
        raise TaskStateConflict(f"任务 {task_id} 当前状态 {task.status} 不可派发")

    if expected_version is not None and expected_version != task.version:
        raise TaskStateConflict("expected_version 不匹配")

    task.awaiting_dispatch = False
    self._apply_initial_status(task)
    task.version += 1
    await self._db.flush()
    self._journal(
        task,
        action="dispatch",
        token_id=token_id,
        principal=principal,
        result="success",
    )
    return task
```

`src/max_agent/kernel/dag.py`（依赖满足时，`awaiting_dispatch` 任务保持 pending）：

```python
async def wake_satisfied(session, task_id: str) -> None:
    """依赖满足后唤醒下游任务。

    派发门：awaiting_dispatch=True 的任务必须保持 pending，
    不得因依赖变化自动 ready —— 解除派发门只能通过 POST /v1/tasks/{id}/dispatch。
    """
    upstream = await session.get(Task, task_id)
    if upstream is None:
        return
    for downstream in await _dependents(session, task_id):
        if downstream.awaiting_dispatch:
            # 仍在派发门内：保持 pending，不自动就绪
            downstream.status = "pending"
            continue
        ...  # 既有唤醒逻辑保持不变
```

`src/max_agent/api/routes.py`：

```python
@router.post("/v1/tasks/{task_id}/dispatch", response_model=TaskRead)
async def dispatch_task(
    task_id: str,
    payload: DispatchRequest,
    context: AuthContext = Depends(require_scope(DISPATCH)),
    service=Depends(get_service),
):
    try:
        task = await service.dispatch(
            task_id,
            principal=context.principal,
            token_id=context.token_id,
            expected_version=payload.expected_version,
            content_hash=payload.content_hash,
        )
    except TaskStateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return TaskRead.model_validate(task)
```

**Step 4.4 再跑同一命令**

```bash
uv run --no-sync pytest tests/test_dispatch_gate.py -v
```

预期 PASS。

**Step 4.5 提交**

```bash
git add src/max_agent/kernel/service.py src/max_agent/kernel/dag.py src/max_agent/api/routes.py src/max_agent/models/schemas.py tests/test_dispatch_gate.py
git commit -m "feat(kernel): 引入显式派发门与 dispatch 端点" -m "Refs #65"
```

---

### Task 5：`content_hash` 与 `expected_version` 409、敏感 tag 覆盖、approve/reject 幂等

**Files:**
- Modify: `src/max_agent/kernel/service.py`
- Modify: `src/max_agent/models/schemas.py`
- Modify: `src/max_agent/api/routes.py`
- Create: `tests/test_content_hash_approval.py`

**Interfaces:**

```python
# src/max_agent/kernel/service.py
HASH_FIELDS: tuple[str, ...]   # 固定 13 个，version 不在其中

def canonical_json(payload: Mapping[str, Any]) -> bytes: ...
def content_hash_of(source: Task | Mapping[str, Any]) -> str: ...
def task_content_hash(task: Task) -> str: ...

class TaskStateConflict(ValueError): ...   # HTTP 层映射为 409

async def approve(
    self,
    task_id: str,
    *,
    principal: str | None = None,
    token_id: str | None = None,
    expected_version: int | None = None,
    content_hash: str | None = None,
    scope: list[str] | None = None,
) -> Task: ...

async def reject(
    self,
    task_id: str,
    *,
    principal: str | None = None,
    token_id: str | None = None,
    expected_version: int | None = None,
    content_hash: str | None = None,
) -> Task: ...
```

`approve()` / `reject()` 的判定顺序固定为：**所有权 → content_hash → 幂等 → expected_version → 状态 → 敏感 tag 覆盖 → 应用决策**。

**Step 5.1 写失败测试**

新建 `tests/test_content_hash_approval.py`：

```python
from __future__ import annotations


def _create(client, auth_header, make_task_body, **overrides):
    resp = client.post(
        "/v1/tasks",
        json=make_task_body(**overrides),
        headers=auth_header("test-create"),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _waiting_approval(client, auth_header, make_task_body, **overrides):
    overrides.setdefault("tags", ["payment"])
    task = _create(client, auth_header, make_task_body, **overrides)
    assert task["status"] == "waiting_approval"
    return task


def test_hash_fields_are_exactly_thirteen_and_exclude_version():
    from max_agent.kernel.service import HASH_FIELDS

    assert len(HASH_FIELDS) == 13
    assert "version" not in HASH_FIELDS
    assert set(HASH_FIELDS) == {
        "goal",
        "acceptance_criteria",
        "worker_kind",
        "worker_payload",
        "tags",
        "dependencies",
        "has_side_effects",
        "issue_ref",
        "priority",
        "timeout_seconds",
        "max_attempts",
        "max_cost_units",
        "allow_auto_retry_side_effects",
    }


def test_canonical_json_is_sorted_and_compact():
    from max_agent.kernel.service import canonical_json

    assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_content_hash_is_lowercase_sha256_hex(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body)
    assert len(task["content_hash"]) == 64
    assert task["content_hash"] == task["content_hash"].lower()
    int(task["content_hash"], 16)


def test_content_hash_is_stable_across_identical_tasks(client, auth_header, make_task_body):
    a = _create(client, auth_header, make_task_body, goal="same goal")
    b = _create(client, auth_header, make_task_body, goal="same goal")
    assert a["content_hash"] == b["content_hash"]
    assert a["version"] == b["version"]  # version 不进哈希


def test_content_hash_changes_with_content(client, auth_header, make_task_body):
    a = _create(client, auth_header, make_task_body, goal="goal one")
    b = _create(client, auth_header, make_task_body, goal="goal two")
    assert a["content_hash"] != b["content_hash"]


def test_approve_with_incomplete_sensitive_tag_scope_returns_409(client, auth_header, make_task_body):
    task = _waiting_approval(client, auth_header, make_task_body)
    resp = client.post(
        f"/v1/tasks/{task['id']}/approve",
        json={
            "expected_version": task["version"],
            "content_hash": task["content_hash"],
            "scope": ["delete"],
        },
        headers=auth_header("test-approve"),
    )
    assert resp.status_code == 409
    again = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read")).json()
    assert again["status"] == "waiting_approval"
    assert again["version"] == task["version"]


def test_approve_with_full_sensitive_tag_scope_succeeds(client, auth_header, make_task_body):
    task = _waiting_approval(client, auth_header, make_task_body)
    resp = client.post(
        f"/v1/tasks/{task['id']}/approve",
        json={
            "expected_version": task["version"],
            "content_hash": task["content_hash"],
            "scope": ["payment"],
        },
        headers=auth_header("test-approve"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] != "waiting_approval"
    assert body["version"] == task["version"] + 1


def test_approve_is_idempotent(client, auth_header, make_task_body):
    task = _waiting_approval(client, auth_header, make_task_body)
    payload = {
        "expected_version": task["version"],
        "content_hash": task["content_hash"],
        "scope": ["payment"],
    }
    first = client.post(
        f"/v1/tasks/{task['id']}/approve", json=payload, headers=auth_header("test-approve")
    )
    assert first.status_code == 200
    second = client.post(
        f"/v1/tasks/{task['id']}/approve", json=payload, headers=auth_header("test-approve")
    )
    assert second.status_code == 200
    assert second.json()["version"] == first.json()["version"]


def test_approve_with_stale_version_returns_409(client, auth_header, make_task_body):
    task = _waiting_approval(client, auth_header, make_task_body)
    resp = client.post(
        f"/v1/tasks/{task['id']}/approve",
        json={
            "expected_version": task["version"] + 99,
            "content_hash": task["content_hash"],
            "scope": ["payment"],
        },
        headers=auth_header("test-approve"),
    )
    assert resp.status_code == 409


def test_approve_with_wrong_hash_returns_409(client, auth_header, make_task_body):
    task = _waiting_approval(client, auth_header, make_task_body)
    resp = client.post(
        f"/v1/tasks/{task['id']}/approve",
        json={
            "expected_version": task["version"],
            "content_hash": "0" * 64,
            "scope": ["payment"],
        },
        headers=auth_header("test-approve"),
    )
    assert resp.status_code == 409


def test_reject_uses_approve_scope_and_is_idempotent(client, auth_header, make_task_body):
    task = _waiting_approval(client, auth_header, make_task_body)
    payload = {"expected_version": task["version"], "content_hash": task["content_hash"]}
    first = client.post(
        f"/v1/tasks/{task['id']}/reject", json=payload, headers=auth_header("test-approve")
    )
    assert first.status_code == 200
    second = client.post(
        f"/v1/tasks/{task['id']}/reject", json=payload, headers=auth_header("test-approve")
    )
    assert second.status_code == 200
    assert second.json()["version"] == first.json()["version"]


def test_reject_with_dispatch_token_returns_403(client, auth_header, make_task_body):
    task = _waiting_approval(client, auth_header, make_task_body)
    resp = client.post(
        f"/v1/tasks/{task['id']}/reject",
        json={"expected_version": task["version"], "content_hash": task["content_hash"]},
        headers=auth_header("test-dispatch"),
    )
    assert resp.status_code == 403
```

**Step 5.2 运行测试（提交方执行）**

```bash
uv run --no-sync pytest tests/test_content_hash_approval.py -v
```

预期 FAIL 原因：`max_agent.kernel.service` 里没有 `HASH_FIELDS` / `canonical_json`（ImportError），响应体里没有 `content_hash`。

**Step 5.3 最小实现**

`src/max_agent/kernel/service.py` 新增：

```python
import hashlib
import json
from typing import Any, Mapping

HASH_FIELDS: tuple[str, ...] = (
    "goal",
    "acceptance_criteria",
    "worker_kind",
    "worker_payload",
    "tags",
    "dependencies",
    "has_side_effects",
    "issue_ref",
    "priority",
    "timeout_seconds",
    "max_attempts",
    "max_cost_units",
    "allow_auto_retry_side_effects",
)

_BOOL_HASH_FIELDS = {"has_side_effects", "allow_auto_retry_side_effects"}
_LIST_HASH_FIELDS = {"tags", "dependencies"}


class TaskStateConflict(ValueError):
    """状态 / 版本 / 哈希不匹配；HTTP 层映射为 409。"""


def canonical_json(payload: Mapping[str, Any]) -> bytes:
    """UTF-8、键按字典序排序、无多余空白的 JSON。"""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _field(source: Task | Mapping[str, Any], name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def content_hash_of(source: Task | Mapping[str, Any]) -> str:
    """13 个固定字段的 SHA-256 小写 hex；version 不参与。"""
    material: dict[str, Any] = {}
    for name in HASH_FIELDS:
        value = _field(source, name)
        if name in _LIST_HASH_FIELDS:
            value = list(value or [])
        elif name in _BOOL_HASH_FIELDS:
            value = bool(value)
        material[name] = value
    return hashlib.sha256(canonical_json(material)).hexdigest()


def task_content_hash(task: Task) -> str:
    return content_hash_of(task)
```

`approve()` / `reject()`：

```python
async def approve(
    self,
    task_id: str,
    *,
    principal: str | None = None,
    token_id: str | None = None,
    expected_version: int | None = None,
    content_hash: str | None = None,
    scope: list[str] | None = None,
) -> Task:
    task = await self._get_task(task_id)
    self.authorize_task(task, principal)

    current_hash = content_hash_of(task)
    if content_hash is not None and content_hash != current_hash:
        raise TaskStateConflict("content_hash 不匹配")

    covered = set(scope or [])

    # 幂等：同一 token_id/principal、同一 content_hash、tag 覆盖齐全、决策仍为 granted
    if (
        task.status != "waiting_approval"
        and getattr(task, "approved_by_token_id", None) == token_id
        and getattr(task, "approved_content_hash", None) == current_hash
    ):
        return task

    if expected_version is not None and expected_version != task.version:
        raise TaskStateConflict("expected_version 不匹配")

    if task.status != "waiting_approval":
        raise TaskStateConflict(f"任务 {task_id} 当前状态 {task.status} 不可审批")

    sensitive = set(task.tags or []) & SENSITIVE_TAGS
    missing = sorted(sensitive - covered)
    if missing:
        raise TaskStateConflict(f"审批 tag 范围未覆盖敏感 tag: {missing}")

    self._apply_approval(task, granted=True, token_id=token_id, principal=principal)
    task.version += 1
    await self._db.flush()
    self._journal(
        task,
        action="approve",
        token_id=token_id,
        principal=principal,
        result="success",
    )
    return task


async def reject(
    self,
    task_id: str,
    *,
    principal: str | None = None,
    token_id: str | None = None,
    expected_version: int | None = None,
    content_hash: str | None = None,
) -> Task:
    task = await self._get_task(task_id)
    self.authorize_task(task, principal)

    current_hash = content_hash_of(task)
    if content_hash is not None and content_hash != current_hash:
        raise TaskStateConflict("content_hash 不匹配")

    # 幂等：已是 rejected 且哈希一致
    if task.status == "rejected":
        return task

    if expected_version is not None and expected_version != task.version:
        raise TaskStateConflict("expected_version 不匹配")

    if task.status != "waiting_approval":
        raise TaskStateConflict(f"任务 {task_id} 当前状态 {task.status} 不可拒绝")

    self._apply_approval(task, granted=False, token_id=token_id, principal=principal)
    task.version += 1
    await self._db.flush()
    self._journal(
        task,
        action="reject",
        token_id=token_id,
        principal=principal,
        result="success",
    )
    return task
```

若现有 Task ORM 没有 `approved_by_token_id` / `approved_content_hash` 两个字段，则改为在既有审批记录/事件里查「同一 token_id 的 granted 决策且哈希一致」；**不得**为此新增数据库列（规格只允许新增 `awaiting_dispatch` 列）。

`src/max_agent/models/schemas.py`：

```python
class ApproveRequest(BaseModel):
    expected_version: int
    content_hash: str
    scope: list[str] = []   # 敏感 tag 范围（不是 API scope）


class RejectRequest(BaseModel):
    expected_version: int
    content_hash: str
```

`src/max_agent/models/schemas.py` 的 `TaskRead` 增加只读哈希（不新增数据库列，哈希即时计算）：

```python
from pydantic import BaseModel, ConfigDict, model_validator


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    ...  # 既有字段
    version: int
    awaiting_dispatch: bool
    content_hash: str

    @model_validator(mode="before")
    @classmethod
    def _inject_content_hash(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            data = dict(data)
            data.setdefault("content_hash", content_hash_of(data))
            return data
        payload = {
            name: getattr(data, name)
            for name in cls.model_fields
            if hasattr(data, name)
        }
        payload["content_hash"] = content_hash_of(payload)
        return payload
```

哈希计算只依赖 ORM 属性，与服务层 `content_hash_of(task)` 取值同源，保证「读到的哈希」与「提交时计算的哈希」一致。

`src/max_agent/api/routes.py`：

```python
@router.post("/v1/tasks/{task_id}/approve", response_model=TaskRead)
async def approve_task(
    task_id: str,
    payload: ApproveRequest,
    context: AuthContext = Depends(require_scope(APPROVE)),
    service=Depends(get_service),
):
    try:
        task = await service.approve(
            task_id,
            principal=context.principal,
            token_id=context.token_id,
            expected_version=payload.expected_version,
            content_hash=payload.content_hash,
            scope=list(payload.scope or []),
        )
    except TaskStateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return TaskRead.model_validate(task)
```

**Step 5.4 再跑同一命令**

```bash
uv run --no-sync pytest tests/test_content_hash_approval.py -v
```

预期 PASS。

**Step 5.5 提交**

```bash
git add src/max_agent/kernel/service.py src/max_agent/models/schemas.py src/max_agent/api/routes.py tests/test_content_hash_approval.py
git commit -m "feat(kernel): 为状态变更加入 content_hash 与乐观并发校验" -m "Refs #65"
```

---

### Task 6：取消语义、终态 409、取消后 finalize 不复活、journal 只记 `token_id`

**Files:**
- Modify: `src/max_agent/kernel/service.py`
- Modify: `src/max_agent/kernel/execution.py`（仅确认行为，不新增杀进程协议）
- Modify: `src/max_agent/models/schemas.py`
- Modify: `src/max_agent/api/routes.py`
- Create: `tests/test_cancel_finalize.py`

**Interfaces:**

```python
# src/max_agent/kernel/service.py
async def cancel(
    self,
    task_id: str,
    *,
    principal: str | None = None,
    token_id: str | None = None,
    expected_version: int | None = None,
    content_hash: str | None = None,
) -> Task: ...
```

`cancel()` 的判定顺序固定为：**所有权 → content_hash → expected_version → 终态检查（409）**。

**Step 6.1 写失败测试**

新建 `tests/test_cancel_finalize.py`：

```python
from __future__ import annotations

import pytest
from sqlalchemy import inspect, text


def _create(client, auth_header, make_task_body, **overrides):
    resp = client.post(
        "/v1/tasks",
        json=make_task_body(**overrides),
        headers=auth_header("test-create"),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_cancel_succeeds_with_matching_version_and_hash(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body)
    resp = client.post(
        f"/v1/tasks/{task['id']}/cancel",
        json={"expected_version": task["version"], "content_hash": task["content_hash"]},
        headers=auth_header("test-cancel"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"


def test_cancel_with_wrong_hash_returns_409(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body)
    resp = client.post(
        f"/v1/tasks/{task['id']}/cancel",
        json={"expected_version": task["version"], "content_hash": "0" * 64},
        headers=auth_header("test-cancel"),
    )
    assert resp.status_code == 409
    again = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("test-read")).json()
    assert again["status"] == task["status"]
    assert again["version"] == task["version"]


def test_cancel_with_stale_version_returns_409(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body)
    resp = client.post(
        f"/v1/tasks/{task['id']}/cancel",
        json={"expected_version": task["version"] + 99, "content_hash": task["content_hash"]},
        headers=auth_header("test-cancel"),
    )
    assert resp.status_code == 409


def test_cancel_on_terminal_task_returns_409(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body)
    first = client.post(
        f"/v1/tasks/{task['id']}/cancel",
        json={"expected_version": task["version"], "content_hash": task["content_hash"]},
        headers=auth_header("test-cancel"),
    )
    assert first.status_code == 200
    second = client.post(
        f"/v1/tasks/{task['id']}/cancel",
        json={
            "expected_version": first.json()["version"],
            "content_hash": task["content_hash"],
        },
        headers=auth_header("test-cancel"),
    )
    assert second.status_code == 409


def test_cancel_requires_cancel_scope(client, auth_header, make_task_body):
    task = _create(client, auth_header, make_task_body)
    for token_id in ("test-read", "test-create", "test-approve", "test-dispatch"):
        resp = client.post(
            f"/v1/tasks/{task['id']}/cancel",
            json={"expected_version": task["version"], "content_hash": task["content_hash"]},
            headers=auth_header(token_id),
        )
        assert resp.status_code == 403, token_id


@pytest.mark.anyio
async def test_finalize_after_cancel_does_not_resurrect_task(db_session, service):
    from max_agent.kernel.execution import StaleFenceError, finalize_in_session
    from max_agent.models.schemas import TaskCreate

    task = await service.create_task(
        principal="tester",
        payload=TaskCreate(goal="cancel me", worker_kind="echo", worker_payload={"cmd": ["echo", "x"]}),
    )
    await service.cancel(task.id, principal="tester")

    with pytest.raises(StaleFenceError):
        await finalize_in_session(
            db_session,
            task_id=task.id,
            outcome="succeeded",
            summary="worker finished after cancel",
            worker_kind="echo",
        )

    await db_session.refresh(task)
    assert task.status == "cancelled"


def _journal_events(db_session) -> list[dict]:
    engine = db_session.get_bind()
    names = [
        name
        for name in inspect(engine).get_table_names()
        if any(key in name.lower() for key in ("journal", "audit", "event"))
    ]
    events: list[dict] = []
    for name in names:
        for row in db_session.execute(text(f"SELECT * FROM {name}")).mappings():
            events.append(
                {
                    k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v))
                    for k, v in row.items()
                }
            )
    return events


def test_journal_records_token_id_and_never_the_secret(client, auth_header, make_task_body, db_session):
    task = _create(client, auth_header, make_task_body, require_dispatch=True)
    resp = client.post(
        f"/v1/tasks/{task['id']}/dispatch",
        json={"expected_version": task["version"], "content_hash": task["content_hash"]},
        headers=auth_header("test-dispatch"),
    )
    assert resp.status_code == 200

    events = _journal_events(db_session)
    blob = repr(events)
    assert "test-dispatch" in blob
    assert "test-dispatch-secret" not in blob
    assert "secret_sha256" not in blob
```

**Step 6.2 运行测试（提交方执行）**

```bash
uv run --no-sync pytest tests/test_cancel_finalize.py -v
```

预期 FAIL 原因：`CancelRequest` 不存在 / cancel 端点未校验 version 与 hash（可能返回 200 而非 409），journal 事件里没有 `token_id`。

**Step 6.3 最小实现**

`src/max_agent/models/schemas.py`：

```python
class CancelRequest(BaseModel):
    expected_version: int
    content_hash: str
```

`src/max_agent/kernel/service.py`：

```python
async def cancel(
    self,
    task_id: str,
    *,
    principal: str | None = None,
    token_id: str | None = None,
    expected_version: int | None = None,
    content_hash: str | None = None,
) -> Task:
    task = await self._get_task(task_id)
    self.authorize_task(task, principal)

    current_hash = content_hash_of(task)
    if content_hash is not None and content_hash != current_hash:
        raise TaskStateConflict("content_hash 不匹配")

    if expected_version is not None and expected_version != task.version:
        raise TaskStateConflict("expected_version 不匹配")

    if task.status in {"succeeded", "failed", "cancelled", "rejected"}:
        # 与现有代码一致：终态任务取消 → ValueError → HTTP 409
        raise TaskStateConflict(f"任务 {task_id} 已处于终态 {task.status}")

    task.status = "cancelled"
    task.cancel_requested_at = datetime.now(timezone.utc)
    task.leased_by = None
    task.lease_expires_at = None
    task.version += 1
    await self._db.flush()
    self._journal(
        task,
        action="cancel",
        token_id=token_id,
        principal=principal,
        result="success",
    )
    return task
```

journal 写入函数必须接受并记录 `token_id` 与 `principal`，**不得**记录明文密钥或 `secret_sha256`：

```python
def _journal(self, task, *, action: str, token_id: str | None, principal: str | None, result: str) -> None:
    self._db.add(
        JournalEntry(
            task_id=task.id,
            action=action,
            result=result,
            token_id=token_id,      # 只记 token id，不记秘密
            principal=principal,
            created_at=datetime.now(timezone.utc),
        )
    )
```

`src/max_agent/api/routes.py`：

```python
@router.post("/v1/tasks/{task_id}/cancel", response_model=TaskRead)
async def cancel_task(
    task_id: str,
    payload: CancelRequest,
    context: AuthContext = Depends(require_scope(CANCEL)),
    service=Depends(get_service),
):
    try:
        task = await service.cancel(
            task_id,
            principal=context.principal,
            token_id=context.token_id,
            expected_version=payload.expected_version,
            content_hash=payload.content_hash,
        )
    except TaskStateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return TaskRead.model_validate(task)
```

`src/max_agent/kernel/execution.py`：**不新增杀进程协议、不接线 `cancel_file`**。只需确认 `finalize_in_session` 在任务状态已不在 `leased`/`running`/`verifying`（例如 `cancelled`）时执行既有 `_reject_fence` 并抛 `StaleFenceError`。若现状已如此，本 Task 对该文件只做断言性注释，不改逻辑。

**Step 6.4 再跑同一命令**

```bash
uv run --no-sync pytest tests/test_cancel_finalize.py -v
```

预期 PASS。

**Step 6.5 提交**

```bash
git add src/max_agent/kernel/service.py src/max_agent/kernel/execution.py src/max_agent/models/schemas.py src/max_agent/api/routes.py tests/test_cancel_finalize.py
git commit -m "feat(kernel): 取消路径加入版本与内容哈希校验并锁定终态语义" -m "Refs #65"
```

---

### Task 7：阶段 1 全量回归与旧客户端兼容

**Files:**
- Modify: `tests/test_api_auth.py`（补旧客户端兼容用例）
- Modify: `tests/test_dispatch_gate.py`（补默认行为兼容用例）

**Interfaces:** 无新增接口；本 Task 只做回归验证与兼容用例固化。

**Step 7.1 写测试（兼容矩阵）**

追加到 `tests/test_dispatch_gate.py`：

```python
def test_legacy_client_default_requires_dispatch_false(client, auth_header, make_task_body):
    """旧 HTTP 客户端（不传 require_dispatch）保持现有自动状态机行为。"""
    body = make_task_body()
    assert "require_dispatch" not in body
    task = _create(client, auth_header, make_task_body)
    assert task["awaiting_dispatch"] is False
    assert task["status"] == "ready"


def test_webhook_creation_semantics_unchanged(client, auth_header, make_task_body):
    """webhook 保持签名认证；创建语义维持自动 _apply_initial_status（require_dispatch 默认 false）。"""
    resp = client.post("/v1/inbound/webhook", json={"goal": "from webhook"})
    assert resp.status_code in (401, 403)
```

追加到 `tests/test_api_auth.py`：

```python
def test_read_scope_token_cannot_be_used_for_writes(client, auth_header, make_task_body):
    resp = client.post("/v1/tasks", json=make_task_body(), headers=auth_header("test-read"))
    assert resp.status_code == 403
```

**Step 7.2 运行测试（提交方执行）**

```bash
uv run --no-sync pytest tests/test_dispatch_gate.py tests/test_api_auth.py -v
```

预期 PASS。

**Step 7.3 阶段 1 全量 pytest**

```bash
uv run --no-sync pytest tests/ -v
```

必须全绿。若存在既有测试仍依赖单枚全权令牌，按 Task 1 Step 1.5 的替换表逐个改为对应 scope 令牌（**不得**放宽规格、**不得**给旧令牌加全权）。

阶段 1 必跑清单：

```bash
uv run --no-sync pytest \
  tests/test_api_auth.py \
  tests/test_token_scopes.py \
  tests/test_dispatch_gate.py \
  tests/test_content_hash_approval.py \
  tests/test_cancel_finalize.py \
  -v
```

**Step 7.4 提交**

```bash
git add tests/test_api_auth.py tests/test_dispatch_gate.py
git commit -m "test(api): 固化旧客户端兼容与阶段 1 回归矩阵" -m "Refs #65"
```

**阶段 1 验收小结（写在 PR 描述里，不新增文件）：** 注册表 + scope 中间件、whoami、全部写端点 scope 矩阵、`require_dispatch`/`awaiting_dispatch`/dispatch 端点、version+hash 校验全部交付，pytest 全绿，**不含任何前端改动**，可独立验收。

---

## 阶段 2：Console UI 与 DAG

阶段 2 只交付**前端 + 文档**，**不改任何后端 Python 代码**（服务端能力已在阶段 1 交付），**不改 Cloudflare Worker**。

阶段 2 交付边界（逐条来自规格）：

- **S2-1 凭据只放内存**：令牌仅存在于 React `useState`；禁止 localStorage / sessionStorage / IndexedDB / cookie / URL query / URL hash / 请求日志持久化。
- **S2-2 多令牌 + scope 可视化**：允许同时持有多枚内存令牌（例如「只读浏览卡」+「审批卡」），按操作类型自动选用对应令牌；若当前会话令牌具备任意写 scope（`create`/`dispatch`/`approve`/`cancel`），UI **必须明确显示其 scope 列表**，不得把它呈现为只读凭据。
- **S2-3 whoami 先行**：登录后第一步调用 `GET /v1/auth/whoami`，拿到 `token_id` / `principal` / `scopes` 之后才启用相应按钮。
- **S2-4 危险操作二次确认**：创建 / 派发 / 审批 / 拒绝 / 取消都要二次确认；确认框至少展示 **goal 摘要（截断）**、**内容哈希短前缀**、**目标状态**。
- **S2-5 禁用按钮只是 UX**：服务端始终强制 scope 与所有权；前端单测必须验证「用错误 scope 触发 UI 动作 → 服务器仍返回 403」。
- **S2-6 审批前必须刷新哈希**：批准操作不得沿用过期页面上的旧哈希；UI 在用户确认前必须用当前详情重新拉取一次哈希。
- **S2-7 取消 409 的处理**：取消返回 409 且任务已是 `cancelled` 时，UI 显示「已完成」并刷新页面，**不反复提交**。
- **S2-8 派发语义**：派发按钮的语义是「解除 `awaiting_dispatch` 并进入既有状态机」，**不是「立即执行」**；派发后显示「等待 worker 领取」，并给出指向 `docs/operations/worker.md` 的相对链接。浏览器不启动 worker，也不引入 cron。
- **S2-9 网络路径**：写操作只打**本机同源 FastAPI**（`/console/` 与 `/v1/*` 同源），**不走 Cloudflare 代理**、不改 Worker 允许列表、不部署 Worker。
- **S2-10 DAG 只读**：节点 = 任务，边 = 依赖；数据来自既有 `dependencies` 字段；**不新增 graph GET 端点**，在浏览器内构图；环状依赖按与后端 `assert_acyclic` 相同的规则判定并拒绝。
- **S2-11 建图用既有端点**：创建图任务使用既有 `POST /v1/tasks/graph`，并置**图级 `require_dispatch=true`**，图内所有新节点都停在 `awaiting_dispatch=true` 的 `pending`，直到被显式派发。
- **S2-12 GUI 阻塞不绕过**：若浏览器验收被环境策略阻塞，在 `docs/console.md` 与 PR 描述中标注「未验收」并给出复现步骤，禁止把未跑通的 UI 写成已验收。

---

### Task 8：前端 API 封装、whoami、内存多令牌与「无持久化」

**Files:**
- Modify: `frontend/src/model.ts`
- Create: `frontend/src/model.test.ts`
- Modify: `frontend/src/main.tsx`（只做状态与事件接线，业务判定全部来自 `model.ts`）

**Interfaces:**

```ts
// frontend/src/model.ts
export type Scope =
  | "read"
  | "create"
  | "dispatch"
  | "approve"
  | "cancel"
  | "execute_inline"
  | "ops";

export type ActionName =
  | "read"
  | "create"
  | "dispatch"
  | "approve"
  | "reject"
  | "cancel";

export interface WhoAmI {
  token_id: string;
  principal: string;
  scopes: Scope[];
}

export interface TaskRead {
  id: string;
  goal: string;
  status: string;
  version: number;
  awaiting_dispatch: boolean;
  content_hash: string;
  dependencies: string[];
  tags: string[];
  created_by: string;
}

export interface TaskCreateBody {
  goal: string;
  acceptance_criteria?: string[];
  worker_kind?: string;
  worker_payload?: Record<string, unknown>;
  tags?: string[];
  dependencies?: string[];
  has_side_effects?: boolean;
  issue_ref?: string;
  priority?: number;
  timeout_seconds?: number;
  max_attempts?: number;
  max_cost_units?: number;
  allow_auto_retry_side_effects?: boolean;
  require_dispatch?: boolean;
}

export interface TaskGraphCreateBody extends TaskCreateBody {
  tasks: TaskCreateBody[];
}

export interface VersionedWrite {
  expected_version: number;
  content_hash: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  constructor(status: number, detail: string);
}

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface ApiClientOptions {
  baseUrl?: string;          // 默认 window.location.origin
  fetchImpl?: FetchLike;    // 便于测试注入
}

export class ApiClient {
  constructor(options?: ApiClientOptions);
  whoami(token: string): Promise<WhoAmI>;
  listTasks(token: string): Promise<TaskRead[]>;
  getTask(token: string, taskId: string): Promise<TaskRead>;
  createTask(token: string, body: TaskCreateBody): Promise<TaskRead>;
  createGraph(token: string, body: TaskGraphCreateBody): Promise<TaskRead[]>;
  dispatchTask(token: string, taskId: string, body: VersionedWrite): Promise<TaskRead>;
  approveTask(
    token: string,
    taskId: string,
    body: VersionedWrite & { scope: string[] },
  ): Promise<TaskRead>;
  rejectTask(token: string, taskId: string, body: VersionedWrite): Promise<TaskRead>;
  cancelTask(token: string, taskId: string, body: VersionedWrite): Promise<TaskRead>;
}

export interface TokenSlot {
  slotId: string;          // 本地槽位名，例如 "浏览卡"
  secret: string;          // 明文令牌，只存在于内存，禁止持久化
  tokenId: string | null;  // whoami 返回
  principal: string | null;
  scopes: Scope[];
}

export function createSlot(slotId: string, secret: string): TokenSlot;
export function applyWhoAmI(slot: TokenSlot, who: WhoAmI): TokenSlot;
export function requiredScope(action: ActionName): Scope;
export function selectSlotForAction(slots: TokenSlot[], action: ActionName): TokenSlot | null;
export function canPerform(slots: TokenSlot[], action: ActionName): boolean;
export async function loadWhoAmI(client: ApiClient, slot: TokenSlot): Promise<TokenSlot>;
export async function loadFreshVersion(
  client: ApiClient,
  token: string,
  taskId: string,
): Promise<VersionedWrite>;
```

**Step 8.1 写失败测试**

新建 `frontend/src/model.test.ts`：

```ts
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ApiClient,
  ApiError,
  applyWhoAmI,
  canPerform,
  createSlot,
  loadFreshVersion,
  loadWhoAmI,
  requiredScope,
  selectSlotForAction,
  type FetchLike,
  type TokenSlot,
  type WhoAmI,
} from "./model";

interface Recorded {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string | null;
}

function jsonResponse(status: number, payload: unknown): Response {
  const body = JSON.stringify(payload);
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => body,
    json: async () => JSON.parse(body),
  } as unknown as Response;
}

function makeFetch(responses: Array<{ status: number; json: unknown }>) {
  const calls: Recorded[] = [];
  const queue = [...responses];
  const impl = vi.fn(async (input: string, init?: RequestInit) => {
    calls.push({
      url: input,
      method: (init?.method ?? "GET").toUpperCase(),
      headers: { ...((init?.headers ?? {}) as Record<string, string>) },
      body: typeof init?.body === "string" ? init.body : null,
    });
    const next = queue.shift() ?? { status: 200, json: {} };
    return jsonResponse(next.status, next.json);
  });
  return { impl: impl as unknown as FetchLike, calls };
}

function installStorageSpies() {
  const stores: Record<string, { setItem: ReturnType<typeof vi.fn> }> = {};
  for (const name of ["localStorage", "sessionStorage"]) {
    const store = {
      setItem: vi.fn(),
      getItem: vi.fn(),
      removeItem: vi.fn(),
      clear: vi.fn(),
      key: vi.fn(),
      length: 0,
    };
    stores[name] = store;
    Object.defineProperty(globalThis, name, {
      value: store,
      configurable: true,
      writable: true,
    });
  }
  const indexedDb = { open: vi.fn(), deleteDatabase: vi.fn() };
  Object.defineProperty(globalThis, "indexedDB", {
    value: indexedDb,
    configurable: true,
    writable: true,
  });
  const cookieSetter = vi.fn();
  const documentStub: Record<string, unknown> = {};
  Object.defineProperty(documentStub, "cookie", { get: () => "", set: cookieSetter });
  Object.defineProperty(globalThis, "document", {
    value: documentStub,
    configurable: true,
    writable: true,
  });
  return { stores, indexedDb, cookieSetter };
}

afterEach(() => {
  delete (globalThis as Record<string, unknown>).localStorage;
  delete (globalThis as Record<string, unknown>).sessionStorage;
  delete (globalThis as Record<string, unknown>).indexedDB;
  delete (globalThis as Record<string, unknown>).document;
});

describe("令牌只存在于内存", () => {
  it("whoami 之后不写任何持久化存储", async () => {
    const spies = installStorageSpies();
    const { impl } = makeFetch([
      { status: 200, json: { token_id: "read-card", principal: "tester", scopes: ["read"] } },
    ]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });

    const slot = await loadWhoAmI(client, createSlot("浏览卡", "read-secret"));

    expect(slot.tokenId).toBe("read-card");
    expect(slot.scopes).toEqual(["read"]);
    expect(spies.stores.localStorage.setItem).not.toHaveBeenCalled();
    expect(spies.stores.sessionStorage.setItem).not.toHaveBeenCalled();
    expect(spies.indexedDb.open).not.toHaveBeenCalled();
    expect(spies.cookieSetter).not.toHaveBeenCalled();
  });

  it("令牌不出现在 URL query / hash 里", async () => {
    const { impl, calls } = makeFetch([
      { status: 200, json: { token_id: "read-card", principal: "tester", scopes: ["read"] } },
    ]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });

    await loadWhoAmI(client, createSlot("浏览卡", "read-secret"));

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("http://127.0.0.1:8765/v1/auth/whoami");
    expect(calls[0].url).not.toContain("read-secret");
    expect(calls[0].headers.Authorization).toBe("Bearer read-secret");
  });
});

describe("写请求只打同源 /v1/*", () => {
  it("create 走同源 POST /v1/tasks", async () => {
    const { impl, calls } = makeFetch([
      { status: 200, json: { id: "t1", goal: "g", status: "pending", version: 1 } },
    ]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });

    await client.createTask("create-secret", { goal: "g", require_dispatch: true });

    expect(calls[0].method).toBe("POST");
    expect(calls[0].url).toBe("http://127.0.0.1:8765/v1/tasks");
    expect(new URL(calls[0].url).pathname.startsWith("/v1/")).toBe(true);
    expect(calls[0].url).not.toContain("create-secret");
  });
});

describe("403 冒泡", () => {
  it("缺 scope 时抛出 ApiError(403)", async () => {
    const { impl } = makeFetch([{ status: 403, json: { detail: "missing scope: create" } }]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });

    await expect(
      client.createTask("read-secret", { goal: "g", require_dispatch: true }),
    ).rejects.toBeInstanceOf(ApiError);

    try {
      await client.createTask("read-secret", { goal: "g", require_dispatch: true });
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).status).toBe(403);
      expect((err as ApiError).detail).toContain("missing scope");
    }
  });
});

describe("多令牌按动作选卡", () => {
  const whoami = (token_id: string, scopes: string[]): WhoAmI => ({
    token_id,
    principal: "tester",
    scopes: scopes as WhoAmI["scopes"],
  });

  it("requiredScope 映射动作到 API scope", () => {
    expect(requiredScope("create")).toBe("create");
    expect(requiredScope("dispatch")).toBe("dispatch");
    expect(requiredScope("approve")).toBe("approve");
    expect(requiredScope("reject")).toBe("approve");
    expect(requiredScope("cancel")).toBe("cancel");
    expect(requiredScope("read")).toBe("read");
  });

  it("按操作类型挑出拥有对应 scope 的令牌", () => {
    const slots: TokenSlot[] = [
      applyWhoAmI(createSlot("浏览卡", "s1"), whoami("read-card", ["read"])),
      applyWhoAmI(createSlot("审批卡", "s2"), whoami("approve-card", ["approve"])),
      applyWhoAmI(createSlot("派发卡", "s3"), whoami("dispatch-card", ["dispatch"])),
    ];

    expect(selectSlotForAction(slots, "read")?.slotId).toBe("浏览卡");
    expect(selectSlotForAction(slots, "approve")?.slotId).toBe("审批卡");
    expect(selectSlotForAction(slots, "reject")?.slotId).toBe("审批卡");
    expect(selectSlotForAction(slots, "dispatch")?.slotId).toBe("派发卡");
    expect(selectSlotForAction(slots, "create")).toBeNull();
    expect(canPerform(slots, "create")).toBe(false);
    expect(canPerform(slots, "approve")).toBe(true);
  });

  it("whoami 的 scopes 不含任何秘密字段", () => {
    const { impl } = makeFetch([
      { status: 200, json: { token_id: "read-card", principal: "tester", scopes: ["read"] } },
    ]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });
    return client.whoami("read-secret").then((who) => {
      expect(Object.keys(who).sort()).toEqual(["principal", "scopes", "token_id"]);
    });
  });
});

describe("确认前刷新版本与哈希", () => {
  it("loadFreshVersion 重新拉取当前任务详情", async () => {
    const { impl, calls } = makeFetch([
      {
        status: 200,
        json: {
          id: "t1",
          goal: "g",
          status: "waiting_approval",
          version: 7,
          awaiting_dispatch: false,
          content_hash: "f".repeat(64),
          dependencies: [],
          tags: ["payment"],
          created_by: "tester",
        },
      },
    ]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });

    const fresh = await loadFreshVersion(client, "read-secret", "t1");

    expect(calls[0].url).toBe("http://127.0.0.1:8765/v1/tasks/t1");
    expect(fresh.expected_version).toBe(7);
    expect(fresh.content_hash).toBe("f".repeat(64));
  });
});
```

**Step 8.2 运行测试（提交方执行）**

```bash
cd frontend && npm test
```

预期 FAIL 原因：`frontend/src/model.ts` 里没有 `ApiClient` / `TokenSlot` / `selectSlotForAction` / `loadFreshVersion` 等导出，测试文件导入即报错（`SyntaxError: The requested module ... does not provide an export named 'ApiClient'`）。

**Step 8.3 最小实现**

`frontend/src/model.ts`（在既有只读 GET 封装之上新增以下内容，**不删除既有导出**）：

```ts
export type Scope =
  | "read"
  | "create"
  | "dispatch"
  | "approve"
  | "cancel"
  | "execute_inline"
  | "ops";

export type ActionName =
  | "read"
  | "create"
  | "dispatch"
  | "approve"
  | "reject"
  | "cancel";

export interface WhoAmI {
  token_id: string;
  principal: string;
  scopes: Scope[];
}

export interface TaskRead {
  id: string;
  goal: string;
  status: string;
  version: number;
  awaiting_dispatch: boolean;
  content_hash: string;
  dependencies: string[];
  tags: string[];
  created_by: string;
}

export interface TaskCreateBody {
  goal: string;
  acceptance_criteria?: string[];
  worker_kind?: string;
  worker_payload?: Record<string, unknown>;
  tags?: string[];
  dependencies?: string[];
  has_side_effects?: boolean;
  issue_ref?: string;
  priority?: number;
  timeout_seconds?: number;
  max_attempts?: number;
  max_cost_units?: number;
  allow_auto_retry_side_effects?: boolean;
  require_dispatch?: boolean;
}

export interface TaskGraphCreateBody extends TaskCreateBody {
  tasks: TaskCreateBody[];
}

export interface VersionedWrite {
  expected_version: number;
  content_hash: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface ApiClientOptions {
  baseUrl?: string;
  fetchImpl?: FetchLike;
}

const DEFAULT_BASE_URL = "http://127.0.0.1:8765";

function extractDetail(payload: unknown): string | null {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail: unknown }).detail;
    if (typeof detail === "string") {
      return detail;
    }
    if (Array.isArray(detail)) {
      return detail
        .map((item) =>
          item && typeof item === "object" && "msg" in item
            ? String((item as { msg: unknown }).msg)
            : JSON.stringify(item),
        )
        .join("; ");
    }
  }
  return null;
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: FetchLike;

  constructor(options: ApiClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
    this.fetchImpl =
      options.fetchImpl ?? ((input, init) => globalThis.fetch(input, init));
  }

  /** 只拼同源 /v1/* 路径；令牌只进 Authorization 头，绝不进 URL。 */
  private url(path: string): string {
    return `${this.baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
  }

  private async request<T>(
    method: string,
    path: string,
    token: string,
    body?: unknown,
  ): Promise<T> {
    const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
    const init: RequestInit = { method, headers };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const response = await this.fetchImpl(this.url(path), init);
    const text = await response.text();
    let parsed: unknown;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = undefined;
      }
    }
    if (!response.ok) {
      throw new ApiError(response.status, extractDetail(parsed) ?? text ?? "");
    }
    return parsed as T;
  }

  whoami(token: string): Promise<WhoAmI> {
    return this.request<WhoAmI>("GET", "/v1/auth/whoami", token);
  }

  listTasks(token: string): Promise<TaskRead[]> {
    return this.request<TaskRead[]>("GET", "/v1/tasks", token);
  }

  getTask(token: string, taskId: string): Promise<TaskRead> {
    return this.request<TaskRead>("GET", `/v1/tasks/${encodeURIComponent(taskId)}`, token);
  }

  createTask(token: string, body: TaskCreateBody): Promise<TaskRead> {
    return this.request<TaskRead>("POST", "/v1/tasks", token, body);
  }

  createGraph(token: string, body: TaskGraphCreateBody): Promise<TaskRead[]> {
    return this.request<TaskRead[]>("POST", "/v1/tasks/graph", token, body);
  }

  dispatchTask(token: string, taskId: string, body: VersionedWrite): Promise<TaskRead> {
    return this.request<TaskRead>(
      "POST",
      `/v1/tasks/${encodeURIComponent(taskId)}/dispatch`,
      token,
      body,
    );
  }

  approveTask(
    token: string,
    taskId: string,
    body: VersionedWrite & { scope: string[] },
  ): Promise<TaskRead> {
    return this.request<TaskRead>(
      "POST",
      `/v1/tasks/${encodeURIComponent(taskId)}/approve`,
      token,
      body,
    );
  }

  rejectTask(token: string, taskId: string, body: VersionedWrite): Promise<TaskRead> {
    return this.request<TaskRead>(
      "POST",
      `/v1/tasks/${encodeURIComponent(taskId)}/reject`,
      token,
      body,
    );
  }

  cancelTask(token: string, taskId: string, body: VersionedWrite): Promise<TaskRead> {
    return this.request<TaskRead>(
      "POST",
      `/v1/tasks/${encodeURIComponent(taskId)}/cancel`,
      token,
      body,
    );
  }
}

export interface TokenSlot {
  slotId: string;
  secret: string;
  tokenId: string | null;
  principal: string | null;
  scopes: Scope[];
}

export function createSlot(slotId: string, secret: string): TokenSlot {
  return { slotId, secret, tokenId: null, principal: null, scopes: [] };
}

export function applyWhoAmI(slot: TokenSlot, who: WhoAmI): TokenSlot {
  return {
    ...slot,
    tokenId: who.token_id,
    principal: who.principal,
    scopes: [...who.scopes],
  };
}

const ACTION_SCOPE: Record<ActionName, Scope> = {
  read: "read",
  create: "create",
  dispatch: "dispatch",
  approve: "approve",
  reject: "approve",
  cancel: "cancel",
};

export function requiredScope(action: ActionName): Scope {
  return ACTION_SCOPE[action];
}

export function selectSlotForAction(
  slots: TokenSlot[],
  action: ActionName,
): TokenSlot | null {
  const scope = requiredScope(action);
  return slots.find((slot) => slot.scopes.includes(scope)) ?? null;
}

export function canPerform(slots: TokenSlot[], action: ActionName): boolean {
  return selectSlotForAction(slots, action) !== null;
}

export async function loadWhoAmI(client: ApiClient, slot: TokenSlot): Promise<TokenSlot> {
  const who = await client.whoami(slot.secret);
  return applyWhoAmI(slot, who);
}

/** 写操作前重新拉取当前详情，拿到最新的 version 与 content_hash。 */
export async function loadFreshVersion(
  client: ApiClient,
  token: string,
  taskId: string,
): Promise<VersionedWrite> {
  const task = await client.getTask(token, taskId);
  return { expected_version: task.version, content_hash: task.content_hash };
}
```

`frontend/src/main.tsx` 的接线要点（只做 UI 状态，不在组件里写业务判定）：

```tsx
const [slots, setSlots] = useState<TokenSlot[]>([]);   // 令牌只在内存
const [client] = useState(() => new ApiClient());

async function addToken(slotId: string, secret: string) {
  const slot = await loadWhoAmI(client, createSlot(slotId, secret));
  setSlots((prev) => [...prev, slot]);
}
```

> 说明：`useState` 的初值函数保证 `ApiClient` 只创建一次；令牌明文只出现在 `slots` 状态与 `Authorization` 头里，**不得**写入 `localStorage` / `sessionStorage` / `IndexedDB` / cookie / URL。

**Step 8.4 再跑同一命令**

```bash
cd frontend && npm test
```

预期 PASS（本 Task 新增用例全绿；既有前端用例不得因此变红）。

**Step 8.5 提交**

```bash
git add frontend/src/model.ts frontend/src/model.test.ts frontend/src/main.tsx
git commit -m "feat(console): 增加写路径封装、whoami 与内存多令牌" -m "Refs #65"
```

---

### Task 9：创建 / 显式派发 / 审批 / 拒绝 / 取消 界面与危险操作确认

**Files:**
- Modify: `frontend/src/model.ts`（补确认框摘要、动作可用性判定、取消 409 归一化）
- Modify: `frontend/src/model.test.ts`（追加本 Task 用例）
- Modify: `frontend/src/main.tsx`（五类动作的按钮 + 确认框 + 刷新逻辑）

**Interfaces:**

```ts
// frontend/src/model.ts（Task 9 新增）
export const SENSITIVE_TAGS: readonly string[] = [
  "delete",
  "deploy",
  "payment",
  "publish",
  "permission",
  "production",
  "external_write",
];

export interface ConfirmSummary {
  goalExcerpt: string;
  hashPrefix: string;
  targetStatus: string;
  lines: string[];
}

export interface ActionAvailability {
  allowed: boolean;
  reason: string | null;
}

export function goalExcerpt(goal: string, maxLength?: number): string;
export function hashPrefix(hash: string | null | undefined, length?: number): string;
export function targetStatusFor(action: ActionName, status?: string): string;
export function buildConfirmSummary(
  input: { goal: string; content_hash?: string | null; status?: string },
  action: ActionName,
): ConfirmSummary;
export function missingSensitiveTags(task: TaskRead, covered: string[]): string[];
export function canApprove(
  task: TaskRead,
  slots: TokenSlot[],
  covered: string[],
): ActionAvailability;
export function canDispatch(task: TaskRead, slots: TokenSlot[]): ActionAvailability;
export function canCancel(task: TaskRead, slots: TokenSlot[]): ActionAvailability;

export type CancelOutcome =
  | { kind: "cancelled"; task: TaskRead }
  | { kind: "already-cancelled"; refresh: true }
  | { kind: "error"; error: ApiError };
export function resolveCancelOutcome(error: unknown): CancelOutcome;
```

**Step 9.1 写失败测试**

追加到 `frontend/src/model.test.ts`（下面这段 import 请**合并到文件顶部的 import 区**，不要在文件中部重复声明）：

```ts
import {
  buildConfirmSummary,
  canApprove,
  canCancel,
  canDispatch,
  hashPrefix,
  missingSensitiveTags,
  resolveCancelOutcome,
  targetStatusFor,
  type TaskRead,
} from "./model";

function taskFixture(overrides: Partial<TaskRead> = {}): TaskRead {
  return {
    id: "t1",
    goal: "把生产环境的计费脚本改成幂等实现",
    status: "waiting_approval",
    version: 3,
    awaiting_dispatch: false,
    content_hash: "a".repeat(64),
    dependencies: [],
    tags: ["payment"],
    created_by: "tester",
    ...overrides,
  };
}

describe("危险操作确认框", () => {
  it("包含 goal 摘要、哈希短前缀与目标状态", () => {
    const summary = buildConfirmSummary(taskFixture(), "approve");

    expect(summary.goalExcerpt.length).toBeLessThanOrEqual(60);
    expect(summary.hashPrefix).toBe("aaaaaaaaaaaa");
    expect(summary.targetStatus).toBe("waiting_approval → approved");
    expect(summary.lines).toHaveLength(3);
    expect(summary.lines.join("\n")).toContain("计费脚本");
    expect(summary.lines.join("\n")).toContain("aaaaaaaaaaaa");
    expect(summary.lines.join("\n")).toContain("approved");
  });

  it("goal 过长时截断", () => {
    const long = "x".repeat(200);
    expect(goalExcerpt(long, 60)).toHaveLength(60);
    expect(goalExcerpt(long, 60).endsWith("…")).toBe(true);
  });

  it("创建动作也走确认框", () => {
    const summary = buildConfirmSummary({ goal: "新建任务" }, "create");
    expect(summary.targetStatus).toContain("pending");
    expect(summary.hashPrefix).toBe("(无)");
  });

  it("五种危险动作都有目标状态", () => {
    expect(targetStatusFor("create")).toContain("pending");
    expect(targetStatusFor("dispatch")).toContain("ready");
    expect(targetStatusFor("approve")).toContain("approved");
    expect(targetStatusFor("reject")).toContain("rejected");
    expect(targetStatusFor("cancel")).toContain("cancelled");
  });

  it("hashPrefix 对空哈希给出占位", () => {
    expect(hashPrefix(null)).toBe("(无)");
    expect(hashPrefix("b".repeat(64))).toBe("bbbbbbbbbbbb");
  });
});

describe("动作可用性判定", () => {
  const approveSlot = applyWhoAmI(createSlot("审批卡", "s"), {
    token_id: "approve-card",
    principal: "tester",
    scopes: ["approve"],
  });
  const dispatchSlot = applyWhoAmI(createSlot("派发卡", "s2"), {
    token_id: "dispatch-card",
    principal: "tester",
    scopes: ["dispatch"],
  });
  const cancelSlot = applyWhoAmI(createSlot("取消卡", "s3"), {
    token_id: "cancel-card",
    principal: "tester",
    scopes: ["cancel"],
  });

  it("敏感 tag 未被覆盖时不允许审批并给出原因", () => {
    const result = canApprove(taskFixture(), [approveSlot], []);
    expect(result.allowed).toBe(false);
    expect(result.reason).toContain("payment");
    expect(missingSensitiveTags(taskFixture(), ["payment"])).toEqual([]);
  });

  it("覆盖敏感 tag 后允许审批", () => {
    expect(canApprove(taskFixture(), [approveSlot], ["payment"]).allowed).toBe(true);
  });

  it("没有 approve scope 时不允许审批", () => {
    expect(canApprove(taskFixture(), [dispatchSlot], ["payment"]).allowed).toBe(false);
  });

  it("派发按钮仅在 awaiting_dispatch=true 且持有 dispatch scope 时可用", () => {
    const gated = taskFixture({ awaiting_dispatch: true, status: "pending" });
    expect(canDispatch(gated, [dispatchSlot]).allowed).toBe(true);
    expect(canDispatch(taskFixture(), [dispatchSlot]).allowed).toBe(false);
    expect(canDispatch(gated, [approveSlot]).allowed).toBe(false);
  });

  it("取消需要 cancel scope", () => {
    expect(canCancel(taskFixture(), [cancelSlot]).allowed).toBe(true);
    expect(canCancel(taskFixture(), [dispatchSlot]).allowed).toBe(false);
  });
});

describe("取消 409 的归一化", () => {
  it("任务已是 cancelled 时视为已完成并刷新", () => {
    const outcome = resolveCancelOutcome(
      new ApiError(409, "任务 t1 已处于终态 cancelled"),
    );
    expect(outcome.kind).toBe("already-cancelled");
    if (outcome.kind === "already-cancelled") {
      expect(outcome.refresh).toBe(true);
    }
  });

  it("其它 409 仍然是错误", () => {
    const outcome = resolveCancelOutcome(new ApiError(409, "expected_version 不匹配"));
    expect(outcome.kind).toBe("error");
  });

  it("非 ApiError 也归为错误", () => {
    const outcome = resolveCancelOutcome(new Error("boom"));
    expect(outcome.kind).toBe("error");
  });
});

describe("禁用按钮不是安全边界", () => {
  it("用只有 read 的令牌触发创建，仍然收到服务器 403", async () => {
    const { impl } = makeFetch([{ status: 403, json: { detail: "missing scope: create" } }]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });

    await expect(
      client.createTask("read-secret", { goal: "g", require_dispatch: true }),
    ).rejects.toMatchObject({ status: 403 });
  });

  it("审批请求体带 expected_version、content_hash 与 tag 覆盖范围", async () => {
    const { impl, calls } = makeFetch([
      { status: 200, json: taskFixture({ status: "ready", version: 4 }) },
    ]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });

    await client.approveTask("approve-secret", "t1", {
      expected_version: 3,
      content_hash: "a".repeat(64),
      scope: ["payment"],
    });

    const sent = JSON.parse(calls[0].body ?? "{}");
    expect(calls[0].method).toBe("POST");
    expect(calls[0].url).toBe("http://127.0.0.1:8765/v1/tasks/t1/approve");
    expect(sent.expected_version).toBe(3);
    expect(sent.content_hash).toBe("a".repeat(64));
    expect(sent.scope).toEqual(["payment"]);
  });

  it("派发与取消请求体都带 expected_version 与 content_hash", async () => {
    const { impl, calls } = makeFetch([
      { status: 200, json: taskFixture() },
      { status: 200, json: taskFixture({ status: "cancelled" }) },
    ]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });

    await client.dispatchTask("dispatch-secret", "t1", {
      expected_version: 3,
      content_hash: "a".repeat(64),
    });
    await client.cancelTask("cancel-secret", "t1", {
      expected_version: 3,
      content_hash: "a".repeat(64),
    });

    expect(calls[0].url).toBe("http://127.0.0.1:8765/v1/tasks/t1/dispatch");
    expect(calls[1].url).toBe("http://127.0.0.1:8765/v1/tasks/t1/cancel");
    expect(JSON.parse(calls[0].body ?? "{}").expected_version).toBe(3);
    expect(JSON.parse(calls[1].body ?? "{}").content_hash).toBe("a".repeat(64));
  });
});
```

**Step 9.2 运行测试（提交方执行）**

```bash
cd frontend && npm test
```

预期 FAIL 原因：`frontend/src/model.ts` 尚未导出 `buildConfirmSummary` / `canApprove` / `canDispatch` / `canCancel` / `resolveCancelOutcome` / `SENSITIVE_TAGS` 等，导入即报 `does not provide an export named ...`。

**Step 9.3 最小实现**

`frontend/src/model.ts` 追加：

```ts
export const SENSITIVE_TAGS: readonly string[] = [
  "delete",
  "deploy",
  "payment",
  "publish",
  "permission",
  "production",
  "external_write",
];

export interface ConfirmSummary {
  goalExcerpt: string;
  hashPrefix: string;
  targetStatus: string;
  lines: string[];
}

export interface ActionAvailability {
  allowed: boolean;
  reason: string | null;
}

export function goalExcerpt(goal: string, maxLength = 60): string {
  const trimmed = (goal ?? "").trim();
  if (trimmed.length <= maxLength) {
    return trimmed;
  }
  return `${trimmed.slice(0, maxLength - 1)}…`;
}

export function hashPrefix(hash: string | null | undefined, length = 12): string {
  if (!hash) {
    return "(无)";
  }
  return hash.slice(0, length);
}

export function targetStatusFor(action: ActionName, status?: string): string {
  switch (action) {
    case "create":
      return "pending（require_dispatch=true，等待显式派发）";
    case "dispatch":
      return `${status ?? "pending"} → ready / blocked / waiting_approval`;
    case "approve":
      return `${status ?? "waiting_approval"} → approved`;
    case "reject":
      return `${status ?? "waiting_approval"} → rejected`;
    case "cancel":
      return `${status ?? "running"} → cancelled`;
    default:
      return status ?? "";
  }
}

export function buildConfirmSummary(
  input: { goal: string; content_hash?: string | null; status?: string },
  action: ActionName,
): ConfirmSummary {
  const goal = goalExcerpt(input.goal);
  const hash = hashPrefix(input.content_hash);
  const target = targetStatusFor(action, input.status);
  return {
    goalExcerpt: goal,
    hashPrefix: hash,
    targetStatus: target,
    lines: [`目标: ${goal}`, `内容哈希: ${hash}`, `目标状态: ${target}`],
  };
}

export function missingSensitiveTags(task: TaskRead, covered: string[]): string[] {
  const tags = new Set(task.tags ?? []);
  const scope = new Set(covered ?? []);
  return [...tags].filter((tag) => SENSITIVE_TAGS.includes(tag) && !scope.has(tag)).sort();
}

export function canApprove(
  task: TaskRead,
  slots: TokenSlot[],
  covered: string[],
): ActionAvailability {
  if (!canPerform(slots, "approve")) {
    return { allowed: false, reason: "当前令牌缺少 approve scope" };
  }
  const missing = missingSensitiveTags(task, covered);
  if (missing.length > 0) {
    return { allowed: false, reason: `审批 tag 范围未覆盖敏感 tag: ${missing.join(", ")}` };
  }
  return { allowed: true, reason: null };
}

export function canDispatch(task: TaskRead, slots: TokenSlot[]): ActionAvailability {
  if (!canPerform(slots, "dispatch")) {
    return { allowed: false, reason: "当前令牌缺少 dispatch scope" };
  }
  if (!task.awaiting_dispatch) {
    return { allowed: false, reason: "任务不在 awaiting_dispatch 状态，无需派发" };
  }
  return { allowed: true, reason: null };
}

export function canCancel(task: TaskRead, slots: TokenSlot[]): ActionAvailability {
  if (!canPerform(slots, "cancel")) {
    return { allowed: false, reason: "当前令牌缺少 cancel scope" };
  }
  return { allowed: true, reason: null };
}

export type CancelOutcome =
  | { kind: "cancelled"; task: TaskRead }
  | { kind: "already-cancelled"; refresh: true }
  | { kind: "error"; error: ApiError };

export function resolveCancelOutcome(error: unknown): CancelOutcome {
  if (error instanceof ApiError && error.status === 409 && /cancelled/i.test(error.detail)) {
    return { kind: "already-cancelled", refresh: true };
  }
  if (error instanceof ApiError) {
    return { kind: "error", error };
  }
  return { kind: "error", error: new ApiError(0, String(error)) };
}
```

`frontend/src/main.tsx` 的五类动作与确认框接线要点：

```tsx
const [confirm, setConfirm] = useState<ConfirmSummary | null>(null);
const [confirmAction, setConfirmAction] = useState<ActionName | null>(null);

// 五类动作统一入口：先弹确认框，用户确认后再执行。
function askConfirm(action: ActionName, task?: TaskRead, draftGoal?: string) {
  const summary = buildConfirmSummary(
    task ? { goal: task.goal, content_hash: task.content_hash, status: task.status } : { goal: draftGoal ?? "" },
    action,
  );
  setConfirm(summary);
  setConfirmAction(action);
}

async function runConfirmed(action: ActionName, task: TaskRead) {
  const slot = selectSlotForAction(slots, action);
  if (!slot) {
    return; // 没有对应 scope 的令牌：按钮置灰，这里只是兜底
  }
  try {
    if (action === "dispatch") {
      // 确认前重新拉取当前 version / content_hash，禁止沿用过期页面上的旧哈希
      const fresh = await loadFreshVersion(client, slot.secret, task.id);
      await client.dispatchTask(slot.secret, task.id, fresh);
    } else if (action === "approve") {
      const fresh = await loadFreshVersion(client, slot.secret, task.id);
      await client.approveTask(slot.secret, task.id, { ...fresh, scope: approveScope });
    } else if (action === "reject") {
      const fresh = await loadFreshVersion(client, slot.secret, task.id);
      await client.rejectTask(slot.secret, task.id, fresh);
    } else if (action === "cancel") {
      const fresh = await loadFreshVersion(client, slot.secret, task.id);
      await client.cancelTask(slot.secret, task.id, fresh);
    }
    await refreshTasks();
  } catch (err) {
    if (action === "cancel") {
      const outcome = resolveCancelOutcome(err);
      if (outcome.kind === "already-cancelled") {
        // 任务已处于 cancelled：显示「已完成」并刷新，不反复提交
        setNotice("任务已处于 cancelled");
        await refreshTasks();
        return;
      }
      throw outcome.error;
    }
    throw err;
  } finally {
    setConfirm(null);
    setConfirmAction(null);
  }
}
```

界面文案要求：

- 派发按钮旁固定显示「派发 ≠ 立即执行；需本机运行 `max-agent worker`」以及相对链接 `docs/operations/worker.md`（S2-8）。
- 令牌区显示每枚令牌的 `token_id` 与 `scopes` 列表；带写 scope 的令牌必须**显式标注其 scope**，不得写成「只读」（S2-2）。
- 审批按钮在敏感 tag 未覆盖时置灰并显示原因（`canApprove(...).reason`），但置灰**只是 UX**，服务端仍会 403/409（S2-5）。

**Step 9.4 再跑同一命令**

```bash
cd frontend && npm test
```

预期 PASS。

**Step 9.5 提交**

```bash
git add frontend/src/model.ts frontend/src/model.test.ts frontend/src/main.tsx
git commit -m "feat(console): 增加创建派发审批拒绝取消界面与危险操作确认" -m "Refs #65"
```

---

### Task 10：DAG 只读视图（复用既有 `dependencies`）

**Files:**
- Create: `frontend/src/dag.ts`
- Create: `frontend/src/dag.test.ts`
- Modify: `frontend/src/main.tsx`（渲染 DAG，保留既有 `dependencies` 文本展示作为兜底）

**Interfaces:**

```ts
// frontend/src/dag.ts
export interface DagNode {
  id: string;
  goal: string;
  status: string;
  awaiting_dispatch: boolean;
}

export interface DagEdge {
  from: string;   // 依赖方（前置任务）
  to: string;     // 依赖它的任务
}

export interface DagGraph {
  nodes: DagNode[];
  edges: DagEdge[];
  levels: Record<string, number>;   // 拓扑层级，用于布局
  cycles: string[][];               // 检出的环（每条是一个节点路径）；无环时为空数组
}

export function buildDag(tasks: TaskRead[]): DagGraph;
export function assertAcyclic(dependencies: Record<string, string[]>): string[] | null;
export function dagCycles(tasks: TaskRead[]): string[][];
```

**Step 10.1 写失败测试**

新建 `frontend/src/dag.test.ts`：

```ts
import { describe, expect, it } from "vitest";

import { assertAcyclic, buildDag, dagCycles } from "./dag";
import type { TaskRead } from "./model";

function task(id: string, dependencies: string[] = [], overrides: Partial<TaskRead> = {}): TaskRead {
  return {
    id,
    goal: `goal ${id}`,
    status: "pending",
    version: 1,
    awaiting_dispatch: true,
    content_hash: "c".repeat(64),
    dependencies,
    tags: [],
    created_by: "tester",
    ...overrides,
  };
}

describe("DAG 构图", () => {
  it("节点与边都来自既有 dependencies 字段", () => {
    const graph = buildDag([task("a"), task("b", ["a"]), task("c", ["a", "b"])]);

    expect(graph.nodes.map((n) => n.id)).toEqual(["a", "b", "c"]);
    expect(graph.edges).toEqual([
      { from: "a", to: "b" },
      { from: "a", to: "c" },
      { from: "b", to: "c" },
    ]);
  });

  it("边方向是 依赖 → 依赖者", () => {
    const graph = buildDag([task("up"), task("down", ["up"])]);
    const edge = graph.edges[0];
    expect(edge.from).toBe("up");
    expect(edge.to).toBe("down");
  });

  it("按依赖层级分层", () => {
    const graph = buildDag([task("a"), task("b", ["a"]), task("c", ["b"])]);
    expect(graph.levels.a).toBe(0);
    expect(graph.levels.b).toBe(1);
    expect(graph.levels.c).toBe(2);
  });

  it("只保留图中出现的依赖边（不新增 graph GET 端点）", () => {
    const graph = buildDag([task("a", ["missing-upstream"])]);
    expect(graph.edges).toEqual([]);
    expect(graph.nodes).toHaveLength(1);
  });

  it("携带节点状态与派发门标记", () => {
    const graph = buildDag([
      task("a", [], { status: "waiting_approval", awaiting_dispatch: false }),
    ]);
    expect(graph.nodes[0].status).toBe("waiting_approval");
    expect(graph.nodes[0].awaiting_dispatch).toBe(false);
  });
});

describe("环状依赖被拒绝", () => {
  it("两节点环能被检出", () => {
    const graph = buildDag([task("a", ["b"]), task("b", ["a"])]);
    expect(graph.cycles.length).toBeGreaterThan(0);
    expect(graph.cycles[0]).toContain("a");
    expect(graph.cycles[0]).toContain("b");
  });

  it("三节点环能被检出", () => {
    const graph = buildDag([task("a", ["c"]), task("b", ["a"]), task("c", ["b"])]);
    expect(graph.cycles.length).toBeGreaterThan(0);
  });

  it("无环时 cycles 为空且 assertAcyclic 返回 null", () => {
    const graph = buildDag([task("a"), task("b", ["a"])]);
    expect(graph.cycles).toEqual([]);
    expect(assertAcyclic({ a: [], b: ["a"] })).toBeNull();
  });

  it("assertAcyclic 返回环路径", () => {
    const cycle = assertAcyclic({ a: ["b"], b: ["a"] });
    expect(cycle).not.toBeNull();
    expect(cycle).toContain("a");
    expect(cycle).toContain("b");
  });

  it("dagCycles 与 buildDag 的判定一致", () => {
    const tasks = [task("a", ["b"]), task("b", ["a"])];
    expect(dagCycles(tasks).length).toBe(buildDag(tasks).cycles.length);
  });
});
```

**Step 10.2 运行测试（提交方执行）**

```bash
cd frontend && npm test
```

预期 FAIL 原因：`frontend/src/dag.ts` 不存在，测试文件导入 `./dag` 报模块解析失败。

**Step 10.3 最小实现**

新建 `frontend/src/dag.ts`：

```ts
import type { TaskRead } from "./model";

export interface DagNode {
  id: string;
  goal: string;
  status: string;
  awaiting_dispatch: boolean;
}

export interface DagEdge {
  from: string;
  to: string;
}

export interface DagGraph {
  nodes: DagNode[];
  edges: DagEdge[];
  levels: Record<string, number>;
  cycles: string[][];
}

/**
 * 环检测规则与后端 max_agent.kernel.dag.assert_acyclic 保持一致：
 * 依赖图必须是有向无环图，出现环即拒绝。
 * 这里只复刻同一条规则，不引入另一套判定标准。
 */
export function assertAcyclic(dependencies: Record<string, string[]>): string[] | null {
  const WHITE = 0;
  const GREY = 1;
  const BLACK = 2;
  const color: Record<string, number> = {};
  const stack: string[] = [];

  const visit = (node: string): string[] | null => {
    color[node] = GREY;
    stack.push(node);
    for (const next of dependencies[node] ?? []) {
      if (!(next in dependencies)) {
        continue;
      }
      if (color[next] === GREY) {
        const start = stack.indexOf(next);
        return [...stack.slice(start), next];
      }
      if ((color[next] ?? WHITE) === WHITE) {
        const cycle = visit(next);
        if (cycle) {
          return cycle;
        }
      }
    }
    stack.pop();
    color[node] = BLACK;
    return null;
  };

  for (const node of Object.keys(dependencies)) {
    if ((color[node] ?? WHITE) === WHITE) {
      const cycle = visit(node);
      if (cycle) {
        return cycle;
      }
    }
  }
  return null;
}

export function dagCycles(tasks: TaskRead[]): string[][] {
  const dependencies: Record<string, string[]> = {};
  for (const task of tasks) {
    dependencies[task.id] = (task.dependencies ?? []).filter((dep) =>
      tasks.some((other) => other.id === dep),
    );
  }
  const cycle = assertAcyclic(dependencies);
  return cycle ? [cycle] : [];
}

export function buildDag(tasks: TaskRead[]): DagGraph {
  const nodes: DagNode[] = tasks.map((task) => ({
    id: task.id,
    goal: task.goal,
    status: task.status,
    awaiting_dispatch: task.awaiting_dispatch,
  }));

  const known = new Set(nodes.map((node) => node.id));
  const edges: DagEdge[] = [];
  for (const task of tasks) {
    for (const dep of task.dependencies ?? []) {
      if (known.has(dep)) {
        edges.push({ from: dep, to: task.id });
      }
    }
  }

  const dependencies: Record<string, string[]> = {};
  for (const task of tasks) {
    dependencies[task.id] = (task.dependencies ?? []).filter((dep) => known.has(dep));
  }

  const levels: Record<string, number> = {};
  const computeLevel = (id: string, seen: Set<string>): number => {
    if (levels[id] !== undefined) {
      return levels[id];
    }
    if (seen.has(id)) {
      return 0; // 环：层级无意义，取 0
    }
    seen.add(id);
    const parents = dependencies[id] ?? [];
    const level =
      parents.length === 0
        ? 0
        : Math.max(...parents.map((parent) => computeLevel(parent, seen))) + 1;
    seen.delete(id);
    levels[id] = level;
    return level;
  };
  for (const node of nodes) {
    computeLevel(node.id, new Set<string>());
  }

  return { nodes, edges, levels, cycles: dagCycles(tasks) };
}
```

`frontend/src/main.tsx` 的 DAG 接线要点：

```tsx
const dag = useMemo(() => buildDag(tasks), [tasks]);

// 图是只读的：只渲染，不提供任何图上的写操作。
// 详情页仍保留既有的 dependencies 文本展示作为兜底。
```

建图入口使用既有 `POST /v1/tasks/graph`，并置**图级 `require_dispatch=true`**（S2-11）：

```ts
await client.createGraph(slot.secret, {
  tasks: drafts,
  require_dispatch: true,
});
```

**Step 10.4 再跑同一命令**

```bash
cd frontend && npm test
```

预期 PASS。

**Step 10.5 提交**

```bash
git add frontend/src/dag.ts frontend/src/dag.test.ts frontend/src/main.tsx
git commit -m "feat(console): 增加基于 dependencies 的只读 DAG 视图" -m "Refs #65"
```

---

### Task 11：前端 test + build、Cloudflare 只读回归、`docs/console.md` 与 README

**Files:**
- Create: `tests/test_console_docs.py`
- Modify: `docs/console.md`
- Modify: `README.md`
- **不修改**：`cloudflare/src/worker.mjs`（Cloudflare 仍是只读代理，写路径保持 404/405）

**Interfaces:**

```python
# tests/test_console_docs.py
def test_console_doc_declares_token_registry_and_scopes() -> None: ...
def test_console_doc_declares_require_dispatch_and_worker_link() -> None: ...
def test_console_doc_declares_cancel_limitations() -> None: ...
def test_console_doc_declares_cloudflare_read_only() -> None: ...
def test_readme_points_to_console_doc() -> None: ...
```

**Step 11.1 写失败测试**

新建 `tests/test_console_docs.py`：

```python
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSOLE_DOC = REPO_ROOT / "docs" / "console.md"
README = REPO_ROOT / "README.md"


def _console_doc_text() -> str:
    return CONSOLE_DOC.read_text(encoding="utf-8")


def test_console_doc_declares_token_registry_and_scopes() -> None:
    text = _console_doc_text()
    assert "MAX_AGENT_API_TOKEN_REGISTRY" in text
    for scope in ("read", "create", "dispatch", "approve", "cancel"):
        assert scope in text
    assert "execute_inline" in text
    assert "ops" in text
    assert "whoami" in text
    assert "内存" in text


def test_console_doc_declares_require_dispatch_and_worker_link() -> None:
    text = _console_doc_text()
    assert "require_dispatch" in text
    assert "awaiting_dispatch" in text
    assert "docs/operations/worker.md" in text
    assert "max-agent worker" in text


def test_console_doc_declares_cancel_limitations() -> None:
    text = _console_doc_text()
    assert "残留" in text
    assert "cancel_file" in text


def test_console_doc_declares_cloudflare_read_only() -> None:
    text = _console_doc_text()
    assert "Cloudflare" in text
    assert "只读" in text


def test_readme_points_to_console_doc() -> None:
    text = README.read_text(encoding="utf-8")
    assert "docs/console.md" in text
```

**Step 11.2 运行测试（提交方执行）**

```bash
uv run --no-sync pytest tests/test_console_docs.py -v
```

预期 FAIL 原因：`docs/console.md` 尚未包含 `MAX_AGENT_API_TOKEN_REGISTRY` / `require_dispatch` / `awaiting_dispatch` / worker 链接 / 进程残留说明 / Cloudflare 只读说明，`README.md` 也尚未指向 `docs/console.md`（断言失败）。

**Step 11.3 最小实现**

`docs/console.md` 需要新增/改写的要点（保留既有只读章节）：

```markdown
## 写能力（P1）

Console 现在可以在本机同域调用写接口：创建、显式派发、审批、拒绝、取消。

### 凭据

- 令牌注册表由环境变量 `MAX_AGENT_API_TOKEN_REGISTRY` 指向一个**本机 JSON 文件**；
  文件里保存 `id`、`secret_sha256`（小写 hex）、`scopes`、可选 `enabled`。
- 令牌**只保存在浏览器内存**（不落 localStorage / sessionStorage / IndexedDB / cookie）。
- 可同时持有多枚内存令牌（例如「只读浏览卡」+「审批卡」），按操作类型自动选用。
- 登录后前端调用 `GET /v1/auth/whoami` 获取 `token_id` / `principal` / `scopes`，
  UI 会**明确显示每枚令牌的 scope 列表**；带写 scope 的令牌不会被当作只读口令宣传。
- scope 集合：`read`、`create`、`dispatch`、`approve`、`cancel`（发给 Console）；
  `execute_inline`（`/schedule`、`/queue/run`）与 `ops`（`/aer/retry`）**不发给 Console**。
- 同一枚令牌不得同时拥有 `dispatch` + `approve`，也不得同时拥有 `create` + `approve`；
  注册表加载期会直接拒绝这类组合。

### 创建与显式派发

- Console 创建任务时使用 `require_dispatch=true`：任务停在 `pending` 且 `awaiting_dispatch=true`。
- 「派发」按钮的语义是**解除 `awaiting_dispatch` 并进入既有状态机**，不是「立即执行」。
- 派发后任务进入 `ready`，需要本机常驻 `max-agent worker` 才会被领取执行；
  详见 [worker 操作说明](../operations/worker.md)（文档相对路径 `docs/operations/worker.md`）。
- 浏览器不会启动 worker，也没有内置定时器。

### 审批 / 拒绝 / 取消

- 审批、拒绝、取消都要求携带当前 `expected_version` 与 `content_hash`；
  页面会在你确认前重新拉取一次任务详情，避免用到过期哈希。
- 带敏感 tag（如 `payment`、`deploy`）的任务必须由审批人显式覆盖对应 tag 范围，否则服务端返回 409。
- 取消只是把任务标记为 `cancelled`：**正在运行的 CLI 子进程可能短暂残留**，
  本 P1 **不接线 `cancel_file`**，也不做同步杀进程；这是已知并接受的限制。
- 取消返回 409 且任务已经是 `cancelled` 时，页面视为「已完成」并刷新。

### Cloudflare

Cloudflare 侧仍是**只读**代理，写路径保持被禁止（404/405）；Console 的写操作只打本机同域
`/v1/*`，不经过 Cloudflare Worker，也不修改 Worker 的允许列表。
```

`README.md` 追加一句：

```markdown
Console 现已支持本机同域的受限写路径（创建 / 派发 / 审批 / 拒绝 / 取消），凭据与限制见 [docs/console.md](docs/console.md)。
```

**Step 11.4 再跑同一命令**

```bash
uv run --no-sync pytest tests/test_console_docs.py -v
```

预期 PASS。

**Step 11.5 前端测试与构建（提交方执行）**

```bash
cd frontend && npm test
```

预期 PASS（阶段 2 全部前端用例）。

```bash
cd frontend && npm run build
```

预期 PASS：Vite 构建成功，产物生成；构建日志中不应出现 TypeScript 类型错误。

**Step 11.6 Cloudflare 只读回归（提交方执行）**

```bash
cd cloudflare && npm test
```

（working directory 为 `cloudflare/`；脚本名以 `cloudflare/package.json` 的 `scripts` 为准，
若脚本名为 `test` 则用 `npm test`，若为其它名字则用 `npm run <脚本名>`。）

预期 PASS：既有 Cloudflare 用例仍然断言写路径被禁止（`404` / `405`），未被本阶段放宽。

同时确认本阶段没有改动 Worker：

```bash
git diff --exit-code -- cloudflare/src/worker.mjs
```

预期：无输出，退出码 `0`（文件未被修改）。

**Step 11.7 提交**

```bash
git add tests/test_console_docs.py docs/console.md README.md
git commit -m "docs(console): 记录写路径能力、令牌发放建议与已知限制" -m "Refs #65"
```

**阶段 2 验收小结（写在 PR 描述里，不新增文件）：** 前端提供创建 / 显式派发 / 审批 / 拒绝 / 取消五类动作，内存多令牌 + whoami + scope 可视化，危险操作二次确认（goal 摘要 / 哈希短前缀 / 目标状态），基于既有 `dependencies` 的只读 DAG 视图；`npm test` 与 `npm run build` 全绿；Cloudflare 写路径仍被禁止（404/405）；`docs/console.md` 已更新写能力与已知限制（含进程短暂残留）。**若浏览器实机验收被环境策略阻塞，必须在此处标注「未验收」并给出复现步骤（G13）。**

---

## 回归与兼容

本节固定「不破坏既有行为」的验证口径。阶段 2 只改前端与文档，**不改后端 Python 代码、不改 Cloudflare Worker**；因此阶段 1 的 pytest 清单必须在阶段 2 结束后重跑并保持全绿。

### R1 旧 HTTP 客户端默认 `require_dispatch=false`

- **口径**：`require_dispatch` 是**新增的可选布尔**，默认 `false`。不传该字段的旧客户端（CLI、MCP、旧 HTTP 脚本、webhook）行为**完全不变**：创建后照旧走 `_apply_initial_status`，进入 `ready` / `blocked` / `waiting_approval`；`awaiting_dispatch` 为 `false`。只有 Console 显式发送 `require_dispatch=true` 时才启用派发门。
- **固化测试**：`tests/test_dispatch_gate.py::test_legacy_client_default_requires_dispatch_false`（Task 7 已写入），断言不传 `require_dispatch` 时 `awaiting_dispatch is False` 且 `status == "ready"`。
- **webhook**：`POST /v1/inbound/webhook` 仍是签名认证，不进入 Bearer scope 体系，创建语义维持自动 `_apply_initial_status`（`tests/test_dispatch_gate.py::test_webhook_creation_semantics_unchanged`）。
- **回归命令（提交方执行）**：

```bash
uv run --no-sync pytest \
  tests/test_dispatch_gate.py::test_legacy_client_default_requires_dispatch_false \
  tests/test_dispatch_gate.py::test_webhook_creation_semantics_unchanged \
  tests/test_dispatch_gate.py::test_default_create_keeps_auto_ready \
  -v
```

预期 PASS：三条用例全绿。

### R2 测试夹具多令牌

- `tests/conftest.py` 的 `api_token_registry` 是 **autouse** fixture：每个测试用例都把一份临时注册表 JSON 写到 `tmp_path`，并设置 `MAX_AGENT_API_TOKEN_REGISTRY` 与 `MAX_AGENT_API_ACTOR=tester`，同时 **删除** `MAX_AGENT_API_TOKEN`（规格禁止旧环境变量自动全权）。
- 夹具内置 7 枚令牌，各持一个 scope，互不重叠：

| token id | secret | scopes | 用途 |
| --- | --- | --- | --- |
| `test-read` | `test-read-secret` | `read` | 只读 GET、telemetry |
| `test-create` | `test-create-secret` | `create` | `POST /v1/tasks`、`POST /v1/tasks/graph` |
| `test-approve` | `test-approve-secret` | `approve` | `POST /v1/tasks/{id}/approve`、`/reject` |
| `test-dispatch` | `test-dispatch-secret` | `dispatch` | `POST /v1/tasks/{id}/dispatch` |
| `test-cancel` | `test-cancel-secret` | `cancel` | `POST /v1/tasks/{id}/cancel` |
| `test-execute-inline` | `test-execute-inline-secret` | `execute_inline` | `/schedule`、`/queue/run`（**不发给 Console**） |
| `test-ops` | `test-ops-secret` | `ops` | `/aer/retry`（**不发给 Console**） |

- **为什么必须拆多枚**：G3 禁止同一 token id 同时持有 `create` + `approve` 或 `dispatch` + `approve`（自建自批 / 自派自批）。因此原本「同一枚 Bearer 既创建又审批」的旧测试必须改为 `test-create` 与 `test-approve` 两枚令牌——这是**按 scope 精确化的兼容迁移，不是放宽规格**。
- **选卡方式**：统一用 `auth_header("test-xxx")` fixture 生成 `{"Authorization": "Bearer <secret>"}`；不要在测试里直接写死 secret 字符串。
- **回归命令（提交方执行）**：

```bash
uv run --no-sync pytest tests/conftest.py tests/test_token_scopes.py tests/test_api_auth.py -v
```

预期 PASS：注册表加载、401/403 矩阵与 whoami 全绿。

### R3 Cloudflare 现有 404/405 回归

- **口径**：Cloudflare Worker 仍是**只读代理**。阶段 2 不修改 `cloudflare/src/worker.mjs`，不修改其允许列表，不部署 Worker；既有 Cloudflare 用例继续断言写路径被禁止（`404` / `405`）。阶段 1 的 scope 改造**不得**放宽这条断言。
- **回归命令（提交方执行）**：

```bash
cd cloudflare && npm test
```

（working directory 为 `cloudflare/`；脚本名以 `cloudflare/package.json` 的 `scripts` 为准：
`npm test` 对应 `scripts.test`；若脚本名为其它名字则改用 `npm run <脚本名>`。）

预期 PASS：写路径仍是 `404` / `405`，没有变成 `2xx`。

- **确认 Worker 未被改动**：

```bash
git diff --exit-code -- cloudflare/src/worker.mjs
```

预期：无输出，退出码 `0`。

- **确认前端写请求不出现在 Worker 代理路径上**：Task 8 的用例 `写请求只打同源 /v1/*` 断言写请求 URL 形如 `http://127.0.0.1:8765/v1/...`，且令牌不出现在 URL 中。

### R4 全量回归命令清单（提交方执行）

阶段 2 完成后按顺序执行，全部必须通过：

```bash
# 1) 后端 pytest 全量（阶段 1 交付物必须仍然全绿）
uv run --no-sync pytest tests/ -v

# 2) 阶段 1 必跑清单 + 阶段 2 文档用例
uv run --no-sync pytest \
  tests/test_api_auth.py \
  tests/test_token_scopes.py \
  tests/test_dispatch_gate.py \
  tests/test_content_hash_approval.py \
  tests/test_cancel_finalize.py \
  tests/test_console_docs.py \
  -v

# 3) 前端测试
cd frontend && npm test

# 4) 前端构建
cd frontend && npm run build

# 5) Cloudflare 只读回归
cd cloudflare && npm test
```

### R5 兼容边界（明确「没有变」的部分）

| 既有行为 | 本 P1 是否改变 | 依据 |
| --- | --- | --- |
| CLI / MCP 的进程内 `configured_principal()` 语义 | 否 | G19 |
| 旧 `MAX_AGENT_API_TOKEN` 直接当全权口令 | **是**（未登记即 401） | G4、规格「旧全权 token 不能当只读口令」 |
| `require_dispatch` 缺省时自动 `_apply_initial_status` | 否 | G5、R1 |
| `POST /v1/inbound/webhook` 签名认证与创建语义 | 否 | G18、R1 |
| `/schedule`、`/queue/run` 就地执行语义 | 否（且不发给 Console） | G20 |
| `POST /v1/tasks/{id}/cancel` 的既有语义 | 否（只加 scope + version + hash 校验） | G8 |
| Cloudflare Worker 只读允许列表 | 否 | G11、R3 |
| 取消的进程级同步终止 | 否（明确不做） | G8、规格「已知并接受」 |

---

## 自检

### S1 规格逐条对照（阶段 1）

| 规格条目 | 对应 Task |
| --- | --- |
| 令牌注册表加载与 scope 中间件（401/403 判定） | Task 1、Task 2 |
| `GET /v1/auth/whoami`（任一 enabled 令牌，不要求 read，不返回秘密） | Task 1 |
| 全部写端点的 scope 矩阵与负面测试 | Task 2 |
| `require_dispatch` 参数、`awaiting_dispatch` 列、迁移 `g0b2d4e15c33` | Task 3、Task 4 |
| `POST /v1/tasks/{id}/dispatch`（只改状态、不跑 worker、幂等） | Task 4 |
| `expected_version` + `content_hash` 强校验（409 且无状态变更） | Task 5、Task 6 |
| 敏感 tag 覆盖不全 → 409 | Task 5 |
| approve / reject / dispatch 幂等规则 | Task 4、Task 5 |
| cancel 的 version/hash 校验、终态 409 | Task 6 |
| 取消后 finalize 不复活（`_reject_fence` / `StaleFenceError`） | Task 6 |
| journal 只记 `token_id` / `principal`，不记秘密与哈希 | Task 6 |
| 旧 `MAX_AGENT_API_TOKEN` 未登记 → 401 | Task 1、Task 7 |
| `dispatch`+`approve`、`create`+`approve` 组合拒绝加载 | Task 1 |
| `GET /v1/telemetry` 映射为 `read` | Task 2 |
| webhook 保持签名认证 | Task 2、Task 7 |
| dispatch 端点不在 API 进程内执行 worker | Task 4 |
| 阶段 1 pytest 全量 | Task 7 |

### S2 规格逐条对照（阶段 2）

| 规格条目 | 对应 Task |
| --- | --- |
| 令牌仅存在于组件内存状态；无 localStorage / sessionStorage / IndexedDB / cookie 写入 | Task 8（`令牌只存在于内存` 用例） |
| 令牌不进入 URL query / hash / 日志 | Task 8（`令牌不出现在 URL query / hash 里` 用例） |
| 允许同时持有多枚内存令牌，按操作类型自动选用 | Task 8（`多令牌按动作选卡` 用例） |
| 登录后调用 whoami，并按 `scopes` 决定按钮可用性 | Task 8（`loadWhoAmI` / `selectSlotForAction`）、Task 9（`canDispatch` / `canApprove` / `canCancel`） |
| 带写 scope 的令牌必须显式显示 scope，不得宣传为只读 | Task 9（界面文案要求：令牌区显示 `token_id` 与 `scopes`） |
| 用错误 scope 触发 UI 动作 → 服务器仍返回 **403** | Task 8（`403 冒泡`）、Task 9（`禁用按钮不是安全边界`） |
| 危险操作确认框包含 goal 摘要、哈希短前缀、目标状态 | Task 9（`危险操作确认框` 用例 + `buildConfirmSummary`） |
| 创建 / 派发 / 审批 / 拒绝 / 取消五类动作都要二次确认 | Task 9（`askConfirm` / `runConfirmed`） |
| 审批前必须用当前详情重新拉取哈希 | Task 8（`loadFreshVersion`）、Task 9（`runConfirmed` 内的 refresh 调用） |
| 取消 409 且任务已 `cancelled` → 显示「已完成」并刷新，不反复提交 | Task 9（`取消 409 的归一化` 用例 + `resolveCancelOutcome`） |
| DAG 由既有 query 结果在前端构图，不新增 graph GET 端点 | Task 10（`buildDag`，只读 GET 复用） |
| 环状依赖被拒绝（与后端 `assert_acyclic` 同一规则） | Task 10（`环状依赖被拒绝` 用例 + `assertAcyclic` / `dagCycles`） |
| 建图用 `POST /v1/tasks/graph` 且图级 `require_dispatch=true` | Task 10（`createGraph` 调用点） |
| 详情页保留既有 `dependencies` 文本展示作为兜底 | Task 10（main.tsx 接线要点） |
| 写请求只发往同源 `/v1/*`，不出现在 Cloudflare Worker 代理路径上 | Task 8（`写请求只打同源 /v1/*`）、回归 R3 |
| 派发后显示「等待 worker 领取」并链接 `docs/operations/worker.md` | Task 9（界面文案要求）、Task 11（`docs/console.md`） |
| 浏览器不启动 worker、不引入 cron / 定时器 | Task 9（界面文案要求）、Task 11（`docs/console.md`） |
| 前端 `npm test` 与 `npm run build` 全绿 | Task 11（Step 11.5） |
| Cloudflare 写路径仍被禁止（404/405），不部署 Worker | Task 11（Step 11.6）、回归 R3 |
| `docs/console.md` 更新写能力、令牌发放建议、已知限制（进程残留） | Task 11（`tests/test_console_docs.py` + 文档内容） |
| GUI 被环境策略阻塞时明确标注「未验收」，不绕过 | Task 11（阶段 2 验收小结）、G13 |
| 不新增 cron / Goal 规划器 / CF 写代理 / MCP 公网暴露 / 跨主体审批 / graph GET / `content_hash` 列 / 任务 patch 接口 | 全程（Global Constraints G11、R5） |

### S3 接口名一致性

以下名字在规格、阶段 1、阶段 2 中必须完全一致，实现时**只调整调用参数，不改命名**：

| 名字 | 定义位置 | 使用位置 |
| --- | --- | --- |
| `require_dispatch` | `TaskCreate` / `TaskGraphCreate` / `TaskGraphCreateBody` | Task 3/4（后端）、Task 8/10（前端 `TaskCreateBody`） |
| `awaiting_dispatch` | `tasks` 表列 + `TaskRead` | Task 3/4（后端）、Task 8/10（前端 `TaskRead`） |
| `content_hash` | `TaskRead`（只读，即时计算） | Task 5/6（后端）、Task 8/9（前端 `VersionedWrite`） |
| `expected_version` | `ApproveRequest` / `RejectRequest` / `CancelRequest` / `DispatchRequest` | Task 5/6（后端）、Task 8/9（前端 `VersionedWrite`） |
| `GET /v1/auth/whoami` | Task 1 | Task 8（`ApiClient.whoami`） |
| `POST /v1/tasks/{id}/dispatch` | Task 4 | Task 8（`ApiClient.dispatchTask`） |
| `POST /v1/tasks/{id}/approve` / `/reject` / `/cancel` | Task 5 / Task 6 | Task 8（`approveTask` / `rejectTask` / `cancelTask`） |
| `POST /v1/tasks/graph` | 既有端点 | Task 10（`ApiClient.createGraph`，图级 `require_dispatch=true`） |
| `scope`（API scope 七项） | `src/max_agent/api/scopes.py` | Task 2、Task 8 前端 `Scope` |
| `scope`（审批 tag 范围） | `ApproveRequest.scope` | Task 5、Task 9 前端 `approveTask(..., { scope })` |
| `token_id` / `principal` / `scopes` | `AuthContext` 与 whoami 响应 | Task 1、Task 8（`WhoAmI` / `TokenSlot`） |
| `SENSITIVE_TAGS` | `src/max_agent/api/scopes.py` | Task 5（后端）、Task 9（前端同名常量，值一致） |

### S4 完整性声明

- **无 TBD**：本计划全文没有 TBD / TODO / 占位符，也没有「类似 Task N」式的省略引用；每个 Task 都给出精确文件路径、接口签名、可直接粘贴的测试代码、提交方外部执行的命令与红绿预期、以及带 `Refs #65` 的 commit。
- **接口名一致**：S3 表格中的全部名字在阶段 1 与阶段 2 中保持一致；阶段 2 不新增后端接口，只在既有 HTTP 契约之上做前端封装。
- **只改一个计划文件**：本次仅追加本文件（`docs/superpowers/plans/2026-11-13-workspace-p1.md`）的阶段 2、回归与兼容、自检三节；阶段 1 内容未被改写。
- **命令归属**：所有 `pytest` / `npm` 命令均标注为**提交方外部执行**，实现模型不执行 Bash（G14）。
- **测试只跑 fake worker**：后端执行类测试使用假 worker 或纯状态机断言，不调用真实付费 CLI / 真实模型（G12）。
- **计划结束**：本计划到此完整结束，无后续占位章节。
