# Staging smoke and production approval

A deploy decision has two checks, and this repository still does not deploy.

1. CI passed: unit tests, package build, dependency audit, and the staging smoke job.
2. A human approved production. Staging does not need that approval. Production does.

## Pull request CI

`.github/workflows/ci.yml` runs tests, then the `staging-smoke` job (`needs: tests`). That job runs:

```bash
uv run --no-sync python scripts/staging_smoke.py
uv run --no-sync python scripts/restore_drill.py
```

The smoke test calls `/v1/health`, `/v1/ready`, and `POST /v1/tasks` on an in-process app with a temporary SQLite file. It does not contact an external host.

## Manual production gate

`.github/workflows/deploy.yml` is `workflow_dispatch` only.

- Target `staging`: the verify job runs tests, smoke, and the restore drill.
- Target `production`: verify must pass, then the `production-gate` job runs in the GitHub Environment named `production`. The approval input must be the exact string `APPROVE`. The workflow turns that into `approved-by:<github.actor>` for `scripts/deploy_gate.py`.

The gate script exits 0 only when CI is marked passed and the approver login is valid. Its JSON always has `deploy_executed: false`. There is no kubectl, SSH, or hosting deploy step.

If the repository plan allows environment protection rules, add required reviewers on the `production` environment. That is an extra stop in the GitHub UI. If the plan does not allow it, the typed `APPROVE` plus the actor login is the recorded human gate. Either way the workflow does not change production.

Local check, without printing the approval value:

```bash
MAX_AGENT_DEPLOY_APPROVAL=approved-by:YOUR_LOGIN \
  uv run --no-sync python scripts/deploy_gate.py --target production --ci-passed
```

Omit `--ci-passed` and the command exits 2.
