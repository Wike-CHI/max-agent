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
