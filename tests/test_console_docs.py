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


def test_console_doc_declares_gui_unverified_when_blocked() -> None:
    text = _console_doc_text()
    assert "未验收" in text
