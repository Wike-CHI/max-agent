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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    secret_sha256: str


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
            secret_sha256=secret_sha256,
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
