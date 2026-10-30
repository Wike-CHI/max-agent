# Operations

Runbooks for making a MAX-Agent checkout deployable, restorable, and observable. Nothing here performs a production deploy.

| Topic | Document |
| --- | --- |
| Wheel/sdist version, checksums, unsigned provenance | [release.md](release.md) |
| Token scope, dependency audit, secret redaction | [supply-chain.md](supply-chain.md) |
| PostgreSQL rollback, backup, restore drill | [postgres.md](postgres.md) |
| Staging smoke and the production approval gate | [deploy-gates.md](deploy-gates.md) |
| Health, telemetry, SLO targets | [slo.md](slo.md) |
| Grok Bot CLI/MCP/webhook end-to-end drill | [grok-e2e.md](grok-e2e.md) |
| Branch protection when the plan cannot enforce it | [branch-protection.md](branch-protection.md) |

Parent issue: #8. Epic: #1.
