# Contributing to MAX-Agent

## Task First development loop

**Every functional change starts from an Issue.** Small docs-only typos may use an Issue labeled maintenance, or `Refs #1` until issue taxonomy is established.

1. Define the problem, acceptance criteria, dependencies, constraints, safety, and test plan in an Issue.
2. Create a branch from current `master`: `feat/<issue>-<slug>`, `fix/<issue>-<slug>`, or `chore/<issue>-<slug>`.
3. Make focused commits; prefer `feat:`, `fix:`, `test:`, `docs:`, or `chore:`.
4. Open a **draft PR** against `master` early, using the PR template and `Refs #N` while working. Change to `Closes #N` when this PR fully resolves the Issue.
5. Run CI (unit tests, package build, issue-link check) and attach validation evidence. Fix failures before review.
6. After human review and passing checks, merge by squash (recommended), preserving the Issue link. **Do not auto-merge or push directly to master.**
7. On a deliberate signed/reviewed version tag `vX.Y.Z`, the release workflow tests, packages, and creates a GitHub Release. No production deployment happens automatically.
8. Write release notes and, if changing DB schema or execution semantics, a rollback / migration note.

## Local commands

```bash
uv sync --locked --extra dev
uv run --no-sync pytest -q
uv build
```

## Pull request requirements

- PR description must include `Closes #N`, `Fixes #N`, `Resolves #N` or `Refs #N` to pass its issue-link check.
- Official Dependabot PRs may skip the Issue link only when every changed file is under `.github/workflows/**` or is `pyproject.toml` / `uv.lock`; any expanded scope requires an Issue link. Both the event actor and PR author must be `dependabot[bot]`.
- `Refs` links a PR to work without closing the Issue; `Closes` closes the Issue **only when merged to default branch**.
- Include reproducible tests, resulting artifacts and relevant screenshots/logs (redact tokens).
- State external side effects, credential needs, fallback and rollback.
- Do not claim a CI check is enforceable as a merge gate unless branch protection is actually enabled.

## Safety policy

- No actual production deployment, remote mutation, payment, release, or secrets exposure without explicit user authorization.
- Workers must be bounded by path permissions, timeout, execution budget, and an approval boundary.
- External side-effect retries require idempotency or explicit human confirmation.
- Do not run untrusted PR source with broad `GITHUB_TOKEN` permissions or repo secrets.
- CI must use read-only permissions. Release publication is the one narrowly scoped write job.

## Repository limitations

The repository was **private** when this process was established. GitHub REST ruleset read returned HTTP 403 with the message that GitHub Pro or public visibility is required. Branch protection/check enforcement may therefore be unavailable until plan/visibility changes. Do not make the repository public just to gain the feature without explicit approval.

Until a ruleset can require checks, run the local gate and only then squash-merge in the GitHub UI. The script does not merge or push:

```bash
gh pr view <PR> --json reviews,statusCheckRollup > /tmp/max-agent-pr.json
uv run --no-sync python scripts/merge_gate.py --payload /tmp/max-agent-pr.json --target master
```

Details: [docs/operations/branch-protection.md](docs/operations/branch-protection.md).

## Agent (Grok Bot) operating contract

- Read current README, docs, active Issues, PRs and CI status before writing code.
- Create Task IDs and include them in the related Issue/PR when MAX is orchestrating its own development.
- Work in a branch and produce a draft PR; never self-approve or merge.
- CI failures are feedback to debug, not permission to loosen tests.
- Use concise progress updates with changed files, commands, test evidence, blockers and next actions.
