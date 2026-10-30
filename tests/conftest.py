from __future__ import annotations

import os
from pathlib import Path

import pytest

from max_agent.db.session import init_db, reset_engine, session_scope


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
