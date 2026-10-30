"""Dependency lock audit, token permissions, and secret redaction."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from max_agent.ops.audit import audit_lock_text
from max_agent.ops.secrets import install_secret_filter

ROOT = Path(__file__).resolve().parents[1]


def test_repository_lock_is_pypi_with_sha256() -> None:
    errors = audit_lock_text((ROOT / "uv.lock").read_text())
    assert errors == []


def test_git_dependency_without_hash_is_rejected() -> None:
    lock = """
version = 1
revision = 1
requires-python = ">=3.12"

[[package]]
name = "side-channel"
version = "1"
source = { git = "https://example.invalid/side.git" }
"""
    errors = audit_lock_text(lock)
    assert errors
    assert any("side-channel" in item for item in errors)


def test_registry_artifact_missing_sha256_is_rejected() -> None:
    lock = """
version = 1
revision = 1
requires-python = ">=3.12"

[[package]]
name = "plain"
version = "1"
source = { registry = "https://pypi.org/simple" }
wheels = [
  { url = "https://files.pythonhosted.org/packages/aa/plain.whl", hash = "md5:abcd" },
]
"""
    errors = audit_lock_text(lock)
    assert any("plain" in item and "sha256" in item for item in errors)


def test_ci_workflow_stays_read_only() -> None:
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "contents: read" in text
    assert "contents: write" not in text
    assert "secrets." not in text
    assert "scripts/audit_dependencies.py" in text
    assert "scripts/staging_smoke.py" in text
    assert "needs: tests" in text


def test_release_write_is_limited_to_publish_job() -> None:
    text = (ROOT / ".github/workflows/release.yml").read_text()
    assert "permissions:" in text
    assert "contents: read" in text
    assert "contents: write" in text
    publish = text.split("github-release:", 1)[1]
    assert "contents: write" in publish
    verify = text.split("github-release:", 1)[0]
    assert "contents: write" not in verify


def test_logs_redact_injected_secrets(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    token = "ghp_exampletokenvalue123456"
    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setenv("MAX_AGENT_DATABASE_URL", "postgresql://max:supersecretpass@localhost/max")
    install_secret_filter()
    caplog.set_level(logging.INFO)
    logging.getLogger("max_agent.supply").info("token %s url password supersecretpass", token)
    assert token not in caplog.text
    assert "supersecretpass" not in caplog.text
    assert "REDACTED" in caplog.text
