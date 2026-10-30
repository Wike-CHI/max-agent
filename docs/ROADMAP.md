# MAX-Agent — Product Roadmap

> Task First, Agent as Worker. Last reviewed: 2026-10-30.
> Status: v0.5.0 pre-release candidate; version targets are milestones, not committed delivery dates.
> Source of truth for work: GitHub Issues + PRs; source of truth for running tasks: MAX DB.

## Problem / Differentiation

Grok Bot already provides chat, a cloud computer, terminal, and app automation. MAX does **not** reproduce them. MAX is a portable, persistent task kernel with interchangeable workers, verifiable outputs, recovery, approval boundaries, and accounting. Grok Bot is one user-facing adapter.

**North-star outcome:** A user submits a GitHub Issue via Grok Bot; MAX persists it as a Task, dispatches a replaceable Worker, verifies an artifact, and returns Task/PR/CI evidence without the chat session staying alive.

**North-star metrics (after instrumenting):**
- verified task completion rate (succeeded with reproducible evidence / eligible submitted tasks);
- median submit-to-verified elapsed time;
- recovery success rate after worker interruption;
- total external spend and Grok Bot usage per verified task (report missing data as unknown);
- percentage of code-changing tasks traceable Issue → PR → CI → Task.

## Current state — v0.5.0 pre-release candidate

The project has moved beyond the original MVP. Current master includes:

- Task persistence, idempotency, dependencies, budgets, acceptance criteria, atomic leases, crash recovery, DAG scheduling, fair queueing, and approval gates (#2–#4, #7).
- Safe CLI execution in isolated worktrees, Mock/LocalCode workers, and the Claude Code CLI vendor (#4, #26).
- Grok Bot CLI/MCP/webhook entry and a completed real end-to-end drill (#5, #20).
- Execution Journal, AER evidence exports, cost telemetry, API token authentication, and task-level authorization (#6, #19).
- A supervised `max-worker` daemon with task claiming, heartbeats, lease renewal, and interruption recovery (#21).
- CI, release artifacts, checksums/provenance, dependency and deployment gates, SQLite restore coverage, pre-release versioning, and Dependabot automation support (#2, #8, #22, #23; Dependabot PRs #16–#18).
- A passing live Postgres backup/rollback/restore drill with no lost journal events (#32; evidence at [docs/operations/evidence/postgres-restore-2026-10-30.json](operations/evidence/postgres-restore-2026-10-30.json)).

This is **not a production-grade v1.0 announcement**. The `max-worker` daemon has merged via #21. The archived live Postgres restore drill now passes with no lost events (#32). The v0.5.0 GitHub pre-release is tracked separately in #33.

## Milestones (outcome-based, not date-driven)

| Milestone | Outcome / demo | Linked Issues | Current state | Exit gate |
| --- | --- | --- | --- | --- |
| **v0.2 — Delivery + Entry** | Grok Bot creates/runs/gets a safe Task; engineering changes flow via Issue → PR → CI | #2, #5 | Complete | CI passes on Python 3.12/3.13; Task ID and verified result returned |
| **v0.3 — Durable execution** | Recover a leased Task after worker interruption without duplicate external effects | #3 | Complete | Concurrency, expiry, crash-recovery tests, and idempotency policy |
| **v0.4 — Real coding Worker** | Authorized CLI worker changes an isolated worktree and verifies the result | #4, #26 | Complete | Secret redaction, timeout/cancel handling, and archived artifacts |
| **v0.5 — Orchestration + Economics** | DAG, approvals, AER traces, authentication, release gates, and measured execution cost | #6, #7, #8, #19, #20, #23, #33 | Pre-release candidate | v0.5.0 artifacts and pre-release flow verified; live Postgres pass archived |
| **v1.0 — Portable runtime** | Supervised worker operation and a versioned, restorable Postgres deployment | #1, #8, #21, #32 | Not released | All v1.0 exit criteria below pass with evidence and human approval |

Issue #8 delivered engineering and operational gates; it did not deploy a production service. Issue #20's evidence is archived at [docs/operations/evidence/grok-e2e-2026-10-30.md](operations/evidence/grok-e2e-2026-10-30.md). Issue #21 is **complete** and merged; Issue #32 is **complete** with a passing live Postgres drill archived at [docs/operations/evidence/postgres-restore-2026-10-30.json](operations/evidence/postgres-restore-2026-10-30.json).

## Remaining gaps and v1.0 exit criteria

A v1.0 tag is allowed only when all of these conditions are met:

1. **Authentication and authorization:** #19 remains complete; protected APIs require authentication and task authorization, with no public unauthenticated API.
2. **Supervised worker:** #21 remains complete, with acceptance evidence for task claiming, heartbeats, lease renewal, and interruption recovery.
3. **End-to-end proof:** #20 remains complete, and the Grok Bot → Task → Worker → verified result path passes against the release candidate.
4. **Postgres recovery:** #32 remains complete; a real disposable Postgres backup/rollback/restore drill has passed and archived evidence showing no lost journal events. A SQLite-only result or a skipped live drill does not satisfy this gate.
5. **Release health:** Python 3.12/3.13 CI, staging smoke, dependency audit, package build, checksums, and provenance generation are green for the release candidate.
6. **Operations and approval:** rollback/runbook and SLO documentation are current, and a human explicitly approves the v1.0 release. No autonomous production deploy is implied.

## Iteration principles

1. Implement one vertical task completion path before adding more Agent types.
2. Keep TaskService authoritative; workers cannot decide final success.
3. Prevent double execution with leases/fencing and idempotency; do not claim exactly-once remote effects.
4. Grok Bot is **not** required for persistence, replay, or recovery.
5. Treat external paid tokens, Grok Bot quotas, and compute time as separate cost sources.
6. Feature work must include tests, acceptance evidence, security note, and a linked Issue.

## Planned architecture boundary

- **Entry:** Grok Bot calls the CLI `--json` contract or the minimal stdio MCP tools (`docs/skills/grok-max-entry/SKILL.md`). HTTP status/approve and the signed inbound webhook use the same authenticated kernel. No chat UI.
- **Control:** TaskService, Scheduler, DAG, approval/policy, Worker capability registry.
- **Execution:** replaceable Workers, supervised CLI vendors, and the `max-worker` daemon lifecycle delivered in #21.
- **Evidence:** Journal, Verification, AER export, trace/cost telemetry.
- **Storage:** SQLite for local development, Postgres for the concurrent runtime and v1.0 recovery gate.

## Prioritization rationale

Complete the v0.5.0 pre-release in #33 and release-candidate validation before considering v1.0. Introduce expensive model routing only when measurable outcomes exist. A UI/dashboard is optional until the task and evidence APIs stabilize.

## Explicit non-goals

- Re-creating Grok Bot's desktop, chat or computer-use interface.
- Editing the closed-source Grok Bot runtime or assuming undocumented hooks.
- Autonomous production deploy, payment or permissions changes.
- Multi-agent swarm orchestration before a reliable single-worker kernel exists.

## GitHub delivery protocol

See [CONTRIBUTING.md](../CONTRIBUTING.md) for Issue → Branch → PR → CI → Merge → Tag. This document is direction, not a substitute for scoped Issues. Parent epic: #1.
