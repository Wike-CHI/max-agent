"""Check that a PR body traces its change to at least one numbered Issue.

Used by GitHub Actions; does not change remote GitHub state.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import PurePosixPath

# Common close keywords plus Refs for incremental/partial PRs.
# GitHub itself only auto-closes with closes/fixes/resolves on merge to default branch.
ISSUE_LINK = re.compile(
    r"(?im)\b(?:closes?|fix(?:es|ed)?|resolves?|refs?)\s+"
    r"(?:Wike-CHI/max-agent)?#\d+\b"
)
DEPENDABOT_LOGIN = "dependabot[bot]"
DEPENDABOT_ROOT_FILES = {"pyproject.toml", "uv.lock"}


def has_issue_link(body: str) -> bool:
    # Ignore HTML comments and code fences; template examples should not satisfy CI.
    body = re.sub(r"<!--[\s\S]*?-->", "", body)
    body = re.sub(r"```[\s\S]*?```", "", body)
    return bool(ISSUE_LINK.search(body))


def is_dependabot_dependency_path(path: str) -> bool:
    if path in DEPENDABOT_ROOT_FILES:
        return True
    workflow_path = PurePosixPath(path)
    return (
        len(workflow_path.parts) >= 3
        and workflow_path.parts[:2] == (".github", "workflows")
        and ".." not in workflow_path.parts
        and workflow_path.suffix in {".yml", ".yaml"}
    )


def main() -> int:
    body = os.environ.get("PR_BODY", "")
    if has_issue_link(body):
        print("PR issue link: OK")
        return 0

    actor = os.environ.get("GITHUB_ACTOR", "")
    author = os.environ.get("PR_AUTHOR", "")
    if actor == DEPENDABOT_LOGIN and author == DEPENDABOT_LOGIN:
        changed_files = os.environ.get("PR_FILES", "").splitlines()
        if changed_files and all(
            is_dependabot_dependency_path(path) for path in changed_files
        ):
            print(
                "PR issue link: skipped for official Dependabot; "
                "dependency-only changed-file scope OK"
            )
            return 0
        print(
            "Dependabot issue-link exception denied: changed files must be limited "
            "to .github/workflows/**, pyproject.toml, or uv.lock.",
            file=sys.stderr,
        )
        return 1

    print(
        "PR needs a real Issue link in the description: "
        "'Closes #123' (auto-close when merged to master) or 'Refs #123'.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
