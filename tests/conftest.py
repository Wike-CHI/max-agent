from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from max_agent.db.session import init_db, reset_engine, session_scope
from max_agent.workers.cli_adapter import ALLOW_ENV, REQUIRE_REAL_ENV
from max_agent.workers.sandbox import ALLOWED_PASSTHROUGH

_HOST_CLI_NAMES = ("claude", "grok", "codex")
_SECRET_SUFFIXES = ("_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN", "_SECRET", "_PASSWORD")
_SECRET_NAMES = frozenset(
    {
        "MINIMAX_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "XAI_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
    }
)

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
def _isolate_host_cli_and_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep stub/fake tests from inheriting host credentials or real coding CLIs."""
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    monkeypatch.delenv(REQUIRE_REAL_ENV, raising=False)
    for key in list(os.environ):
        if (
            key in ALLOWED_PASSTHROUGH
            or key in _SECRET_NAMES
            or key.startswith(("ANTHROPIC_", "OPENAI_", "XAI_", "GROK_", "CLAUDE_"))
            or key.endswith(_SECRET_SUFFIXES)
        ):
            monkeypatch.delenv(key, raising=False)
    kept: list[str] = []
    for part in os.environ.get("PATH", "").split(os.pathsep):
        if not part:
            continue
        directory = Path(part)
        if any((directory / name).exists() for name in _HOST_CLI_NAMES):
            continue
        kept.append(part)
    monkeypatch.setenv("PATH", os.pathsep.join(kept) or "/usr/bin:/bin")


@pytest.fixture(autouse=True)
def _api_test_credentials(monkeypatch: pytest.MonkeyPatch):
    """The legacy MAX_AGENT_API_TOKEN is no longer an all-powerful credential.

    Issue #65 replaces it with an explicit token registry, so the environment
    variable must not silently grant full access anymore.
    """
    monkeypatch.delenv("MAX_AGENT_API_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def api_token_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


@pytest.fixture()
def auth_header():
    def _make(token_id: str) -> dict[str, str]:
        secret = REGISTRY_TOKENS[token_id]["secret"]
        return {"Authorization": f"Bearer {secret}"}

    return _make


@pytest.fixture()
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


@pytest.fixture(autouse=True)
def _reset_aer_adapter():
    from max_agent.kernel.aer import set_aer_adapter

    set_aer_adapter(None)
    yield
    set_aer_adapter(None)


@pytest.fixture()
def db_url(tmp_path: Path) -> str:
    path = tmp_path / "test.db"
    url = f"sqlite:///{path}"
    os.environ["MAX_AGENT_DATABASE_URL"] = url
    reset_engine()
    init_db(url)
    yield url
    reset_engine()
    os.environ.pop("MAX_AGENT_DATABASE_URL", None)


@pytest.fixture()
def session(db_url: str):
    with session_scope(db_url) as s:
        yield s
