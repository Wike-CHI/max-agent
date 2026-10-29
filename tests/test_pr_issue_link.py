from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_pr_issue_link.py"
SPEC = importlib.util.spec_from_file_location("check_pr_issue_link", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_closes_real_issue():
    assert module.has_issue_link("Implements feature\n\nCloses #2")


def test_refs_real_issue():
    assert module.has_issue_link("Refs Wike-CHI/max-agent#3")


def test_no_issue_fails():
    assert not module.has_issue_link("Documentation and testing only")


def test_template_comment_ignored():
    assert not module.has_issue_link("<!-- Closes #111 -->")


def test_code_fence_ignored():
    assert not module.has_issue_link("```\nCloses #11\n```")
