from __future__ import annotations

import os
from pathlib import Path

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
    monkeypatch.setenv("MAX_AGENT_API_TOKEN", "test-api-token")
    monkeypatch.setenv("MAX_AGENT_API_ACTOR", "user")


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
