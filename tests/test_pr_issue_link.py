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


def run_main(monkeypatch, *, body="", actor="octocat", author="octocat", files=()):
    monkeypatch.setenv("PR_BODY", body)
    monkeypatch.setenv("GITHUB_ACTOR", actor)
    monkeypatch.setenv("PR_AUTHOR", author)
    monkeypatch.setenv("PR_FILES", "\n".join(files))
    return module.main()


def test_dependabot_dependency_update_gets_explicit_exception(monkeypatch, capsys):
    result = run_main(
        monkeypatch,
        actor="dependabot[bot]",
        author="dependabot[bot]",
        files=[".github/workflows/ci.yml", ".github/workflows/release.yml"],
    )

    assert result == 0
    assert "skipped for official Dependabot" in capsys.readouterr().out


def test_dependabot_python_dependency_paths_allowed(monkeypatch):
    result = run_main(
        monkeypatch,
        actor="dependabot[bot]",
        author="dependabot[bot]",
        files=["pyproject.toml", "uv.lock"],
    )

    assert result == 0


def test_human_pr_still_requires_issue_link(monkeypatch):
    assert run_main(monkeypatch, files=["uv.lock"]) == 1


def test_spoofed_dependabot_author_cannot_bypass(monkeypatch):
    assert (
        run_main(
            monkeypatch,
            actor="dependabot[bot]",
            author="octocat",
            files=["uv.lock"],
        )
        == 1
    )


def test_spoofed_dependabot_actor_cannot_bypass(monkeypatch):
    assert (
        run_main(
            monkeypatch,
            actor="octocat",
            author="dependabot[bot]",
            files=["uv.lock"],
        )
        == 1
    )


def test_dependabot_expanded_file_set_requires_issue_link(monkeypatch):
    assert (
        run_main(
            monkeypatch,
            actor="dependabot[bot]",
            author="dependabot[bot]",
            files=["uv.lock", "src/max_agent/cli/main.py"],
        )
        == 1
    )


def test_dependabot_empty_file_set_cannot_bypass(monkeypatch):
    assert (
        run_main(
            monkeypatch,
            actor="dependabot[bot]",
            author="dependabot[bot]",
        )
        == 1
    )


def test_out_of_scope_dependabot_pr_with_issue_link_passes(monkeypatch):
    assert (
        run_main(
            monkeypatch,
            body="Closes #22",
            actor="dependabot[bot]",
            author="dependabot[bot]",
            files=["src/max_agent/cli/main.py"],
        )
        == 0
    )
