# Supply chain and tokens

## Workflow permissions

- CI (`.github/workflows/ci.yml`) sets `permissions: contents: read` and does not read repository secrets.
- The release verify job stays read-only. Only the `github-release` job sets `contents: write`, and only for tag publishes.
- The deploy gate workflow is read-only and has no deploy step.

## Human tokens

Prefer `gh` auth with a fine-grained PAT, not a classic token:

- Contents: Read and write (push a feature branch)
- Pull requests: Read and write (open the draft PR)
- Workflows: Read and write only when the branch changes `.github/workflows`
- Metadata: Read

Do not grant Administration, Secrets, or the ability to change repository visibility. CI must not receive that PAT. `GITHUB_TOKEN` inside Actions is enough, with the permissions above.

## Dependency audit

`uv.lock` is the allow-list. Every non-editable package must come from `https://pypi.org/simple` and every sdist/wheel must have a `sha256` hash.

```bash
uv run --no-sync python scripts/audit_dependencies.py
```

CI runs that command. Dependabot (`.github/dependabot.yml`) opens weekly update PRs for `uv` and GitHub Actions. A CVE review still needs a human; this audit does not call an external vulnerability API.

## Secrets in logs

Inject secrets through the environment (`GITHUB_TOKEN`, `MAX_AGENT_WEBHOOK_SECRET`, `PGPASSWORD`, provider API keys). The process installs a root log filter that replaces those values with `[REDACTED]`. Database URLs keep their password out of `pg_dump` argv; the password is passed only as `PGPASSWORD` and is not printed.

Do not pass secrets as command-line arguments. Health and readiness bodies are a fixed set of fields and do not echo the environment.
