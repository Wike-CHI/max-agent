"""Check that a PR body traces its change to at least one numbered Issue.

Used by GitHub Actions; does not change remote GitHub state.
"""

from __future__ import annotations

import os
import re
import sys

# Common close keywords plus Refs for incremental/partial PRs.
# GitHub itself only auto-closes with closes/fixes/resolves on merge to default branch.
ISSUE_LINK = re.compile(
    r"(?im)\b(?:closes?|fix(?:es|ed)?|resolves?|refs?)\s+"
    r"(?:Wike-CHI/max-agent)?#\d+\b"
)


def has_issue_link(body: str) -> bool:
    # Ignore HTML comments and code fences; template examples should not satisfy CI.
    body = re.sub(r"<!--[\s\S]*?-->", "", body)
    body = re.sub(r"```[\s\S]*?```", "", body)
    return bool(ISSUE_LINK.search(body))


def main() -> int:
    body = os.environ.get("PR_BODY", "")
    if has_issue_link(body):
        print("PR issue link: OK")
        return 0
    print(
        "PR needs a real Issue link in the description: "
        "'Closes #123' (auto-close when merged to master) or 'Refs #123'.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
