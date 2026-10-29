# Branch protection when the plan cannot enforce it

This repository was private when the delivery process was written. Reading rulesets can return HTTP 403 with a message that GitHub Pro or public visibility is required. Do not make the repository public just to turn on branch protection.

Until a ruleset can require the checks below, use this procedure. It is executable, and it does not push to `master`.

Required CI checks on a pull request:

- `issue-link`
- `test-py3.12`
- `test-py3.13`
- `staging-smoke`

Steps:

1. Work on `feat/<issue>-<slug>`, `fix/<issue>-<slug>`, or `chore/<issue>-<slug>`. Open a draft PR. Do not push commits to `master`.
2. Wait until those checks are green.
3. Get a human review whose latest state is `APPROVED`. A later `CHANGES_REQUESTED` from the same person clears the approval.
4. Save the pull request payload and run the local gate:

```bash
gh pr view <PR> --json reviews,statusCheckRollup > /tmp/max-agent-pr.json
uv run --no-sync python scripts/merge_gate.py --payload /tmp/max-agent-pr.json --target master
```

The script exits 2 when checks are missing, pending, or failed, or when no reviewer is currently `APPROVED`. On success it prints `"merged": false`. It does not call `gh pr merge` and it does not push.

5. A human squash-merges in the GitHub UI only after the script exits 0.
6. Production stays on the separate deploy gate in [deploy-gates.md](deploy-gates.md). Merging to `master` does not deploy.

When the plan later allows rulesets, require the same four checks and one approving review, and forbid force-pushes to `master`. Keep the script as a local rehearsal either way.
