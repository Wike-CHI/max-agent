"""Bearer authentication and single-user task authorization for the HTTP API.

认证层以本机令牌注册表（`max_agent.api.token_registry`）为准：Bearer 明文只在
注册表里比对 sha256，绝不再有「单个环境变量即全权」的旁路。
"""

from __future__ import annotations

import os
from typing import Callable

from fastapi import Header, HTTPException
from sqlalchemy.orm import Session

from max_agent.db.models import TaskRow

from .scopes import READ
from .token_registry import AuthContext, authenticate

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}


def configured_principal() -> str:
    """Single-user owner from MAX_AGENT_API_ACTOR.

    进程内调用方（CLI / MCP / webhook）沿用这一语义；HTTP 层以令牌注册表为准。
    """
    return os.environ.get("MAX_AGENT_API_ACTOR", "user").strip()[:128] or "user"


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="valid bearer token required",
        headers=_UNAUTHORIZED_HEADERS,
    )


def require_any_token(
    authorization: str | None = Header(default=None),
) -> AuthContext:
    """任一已注册且 enabled 的令牌即可通过；不要求 read，不返回任何秘密。"""
    context = authenticate(_extract_bearer(authorization))
    if context is None:
        raise _unauthorized()
    return context


def require_scope(scope: str) -> Callable[..., AuthContext]:
    """生成要求某个 API scope 的路由依赖。

    未注册 / 已禁用 / 注册表不可用 → 401；已认证但缺少 scope → 403。
    """

    def dependency(authorization: str | None = Header(default=None)) -> AuthContext:
        context = authenticate(_extract_bearer(authorization))
        if context is None:
            raise _unauthorized()
        if scope not in context.scopes:
            raise HTTPException(status_code=403, detail=f"missing scope: {scope}")
        return context

    return dependency


# 兼容既有调用点：语义等价于「需要 read scope」。
require_principal = require_scope(READ)


def require_actor_match(principal: str, requested: str | None) -> None:
    if requested is not None and requested.strip() != principal:
        raise HTTPException(status_code=403, detail="actor must match authenticated principal")


def authorize_task(session: Session, task_id: str, principal: str) -> TaskRow:
    row = session.get(TaskRow, task_id)
    if row is None:
        raise KeyError(f"task not found: {task_id}")
    authorize_task_row(row, principal)
    return row


def authorize_task_row(row: TaskRow, principal: str) -> None:
    if row.created_by != principal:
        raise HTTPException(status_code=403, detail="task access denied")
