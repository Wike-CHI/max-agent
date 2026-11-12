"""Document vs code post-CLI acceptance. No real coding CLI is started."""

from __future__ import annotations

from pathlib import Path

import pytest

from max_agent.models.enums import TaskStatus
from max_agent.workers.document_acceptance import evaluate_document_acceptance
from tests.test_cli_worker import _outputs, _run


def _doc_seed(tmp_path: Path, *, text: str, name: str = "技术选型短方案.md") -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / name).write_text(text, encoding="utf-8")
    return seed


COMPLETE_DOC = """# 技术选型短方案（草案，供审阅）

## 1. 基线与本轮依据
SHA 示例。

## 4. 候选组合
组合 A、B、C。

## 5. 推荐
推荐组合 A。

## 6. 轻量开销、平台支持、Python 音频依赖、迁移成本
目标值，非实测。

## 7. Agent 的形态：任务/过程导向
目标到验收。

## 8. 权限、预算、人工确认、回放证据、CI 门禁
设计建议。

## 9. 本产品业务 Agent 框架 vs MAX 开发编排器
必须区分。
"""

REQUIRED = [
    "# 技术选型短方案",
    "## 5. 推荐",
    "## 6. 轻量开销、平台支持、Python 音频依赖、迁移成本",
    "## 7. Agent 的形态：任务/过程导向",
    "## 8. 权限、预算、人工确认、回放证据、CI 门禁",
    "## 9. 本产品业务 Agent 框架 vs MAX",
]


def _doc_payload(tmp_path: Path, seed: Path) -> dict:
    return {
        "vendor": "stub",
        "workspace_root": str(tmp_path / "ws"),
        "repo_path": str(seed),
        "acceptance": {
            "kind": "document",
            "files": [
                {
                    "path": "技术选型短方案.md",
                    "min_bytes": 50,
                    "min_lines": 8,
                    "max_lines": 200,
                    "required_headings": REQUIRED,
                    "require_complete": True,
                }
            ],
        },
    }


def test_evaluate_document_acceptance_rejects_missing_empty_and_incomplete(tmp_path: Path) -> None:
    missing = evaluate_document_acceptance(
        tmp_path,
        [{"path": "nope.md", "min_bytes": 1, "required_headings": ["# Title"], "require_complete": True}],
    )
    assert missing.ok is False
    assert missing.failure == "document_missing"

    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")
    blank = evaluate_document_acceptance(tmp_path, [{"path": "empty.md", "min_bytes": 1}])
    assert blank.ok is False
    assert blank.failure in {"document_empty", "document_too_short"}

    truncated = tmp_path / "cut.md"
    truncated.write_text("# 技术选型短方案\n\n## 5. 推荐\n建议放到「轻量化实测数据出来之后」再评估。\n≥ 4 个文件全部", encoding="utf-8")
    cut = evaluate_document_acceptance(
        tmp_path,
        [{"path": "cut.md", "min_bytes": 10, "required_headings": ["# 技术选型短方案", "## 5. 推荐"], "require_complete": True}],
    )
    assert cut.ok is False
    assert cut.failure == "document_incomplete"

    ok = evaluate_document_acceptance(
        tmp_path,
        [{"path": "ok.md", "min_bytes": 10, "min_lines": 3, "required_headings": ["# 技术选型短方案"], "require_complete": True}],
    )
    # file not written yet
    assert ok.failure == "document_missing"
    (tmp_path / "ok.md").write_text("# 技术选型短方案\n\n完整收尾。\n", encoding="utf-8")
    passed = evaluate_document_acceptance(
        tmp_path,
        [{"path": "ok.md", "min_bytes": 10, "min_lines": 3, "required_headings": ["# 技术选型短方案"], "require_complete": True}],
    )
    assert passed.ok is True
    assert passed.failure is None


def test_document_task_does_not_run_fixture_test_calc(session, tmp_path: Path) -> None:
    seed = _doc_seed(tmp_path, text=COMPLETE_DOC)
    assert not (seed / "test_calc.py").exists()
    _created, out = _run(
        session,
        "write a short tech selection draft",
        _doc_payload(tmp_path, seed),
        criteria=["worker_ok"],
    )
    outputs = _outputs(session, out.id)
    assert outputs.get("adapter_failure") != "pytest_failed"
    assert outputs.get("pytest_exit_code") is None
    assert "test_calc.py" not in (outputs.get("pytest_stdout") or "")
    assert out.status == TaskStatus.SUCCEEDED, out.last_error
    assert outputs.get("document_acceptance", {}).get("ok") is True


def test_document_task_fails_when_required_heading_missing(session, tmp_path: Path) -> None:
    seed = _doc_seed(tmp_path, text="# 技术选型短方案\n\n只有标题，没有推荐章节。\n")
    _created, out = _run(
        session,
        "incomplete draft",
        _doc_payload(tmp_path, seed),
        criteria=["worker_ok"],
    )
    outputs = _outputs(session, out.id)
    assert out.status == TaskStatus.FAILED
    assert outputs.get("adapter_failure") == "document_heading_missing" or str(outputs.get("adapter_failure", "")).startswith("document_")
    assert outputs.get("pytest_exit_code") is None


def test_code_task_still_runs_workspace_pytest(session, tmp_path: Path) -> None:
    seed = tmp_path / "code-seed"
    seed.mkdir()
    (seed / "calc.py").write_text(
        "def add(a: int, b: int) -> int:\n    raise NotImplementedError(\'fixture issue: implement add\')\n",
        encoding="utf-8",
    )
    (seed / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    _created, out = _run(
        session,
        "implement add",
        {
            "vendor": "stub",
            "workspace_root": str(tmp_path / "code-ws"),
            "repo_path": str(seed),
            "acceptance": {"kind": "code"},
        },
        criteria=["worker_ok"],
    )
    outputs = _outputs(session, out.id)
    assert out.status == TaskStatus.SUCCEEDED, out.last_error
    assert outputs.get("pytest_exit_code") == 0
    assert outputs.get("adapter_failure") in (None, "")


def test_code_task_without_pytest_targets_is_not_silent_success(session, tmp_path: Path) -> None:
    """A real-repo code task with no acceptance and no test_calc.py must not succeed."""
    seed = tmp_path / "repo"
    seed.mkdir()
    (seed / "README.md").write_text("application source without tests\n", encoding="utf-8")
    (seed / "app.py").write_text("print('ok')\n", encoding="utf-8")
    assert not (seed / "test_calc.py").exists()
    _created, out = _run(
        session,
        "review this repo",
        {
            "vendor": "stub",
            "workspace_root": str(tmp_path / "ws"),
            "repo_path": str(seed),
        },
        criteria=["worker_ok"],
    )
    outputs = _outputs(session, out.id)
    assert out.status != TaskStatus.SUCCEEDED
    assert out.status == TaskStatus.FAILED
    assert outputs.get("adapter_failure") == "verification_unspecified"
    assert outputs.get("pytest_exit_code") is None
    verification = outputs.get("verification") or {}
    assert verification.get("status") in {"unspecified", "unverified"}


def test_explicit_unverified_acceptance_is_not_success(session, tmp_path: Path) -> None:
    seed = tmp_path / "repo"
    seed.mkdir()
    (seed / "README.md").write_text("no tests on purpose\n", encoding="utf-8")
    _created, out = _run(
        session,
        "record unverified on purpose",
        {
            "vendor": "stub",
            "workspace_root": str(tmp_path / "ws"),
            "repo_path": str(seed),
            "acceptance": {"kind": "unverified"},
        },
        criteria=["worker_ok"],
    )
    outputs = _outputs(session, out.id)
    assert out.status == TaskStatus.FAILED
    assert outputs.get("adapter_failure") == "verification_unspecified"
    verification = outputs.get("verification") or {}
    assert verification.get("status") in {"unspecified", "unverified"}
    assert verification.get("explicit") is True


def test_missing_explicit_pytest_target_fails(session, tmp_path: Path) -> None:
    seed = tmp_path / "repo"
    seed.mkdir()
    (seed / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _created, out = _run(
        session,
        "run missing tests",
        {
            "vendor": "stub",
            "workspace_root": str(tmp_path / "ws"),
            "repo_path": str(seed),
            "acceptance": {"kind": "code", "pytest_targets": ["tests/test_missing.py"]},
        },
        criteria=["worker_ok"],
    )
    outputs = _outputs(session, out.id)
    assert out.status == TaskStatus.FAILED
    assert outputs.get("adapter_failure") == "pytest_target_missing"
    assert outputs.get("pytest_exit_code") is None
    verification = outputs.get("verification") or {}
    assert "tests/test_missing.py" in (verification.get("missing") or [])
