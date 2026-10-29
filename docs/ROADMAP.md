# MAX-Agent — Product Roadmap

> Task First, Agent as Worker. Last reviewed: 2026-10-29.
> Status: planning; version targets are milestones, not committed delivery dates.
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

## Already shipped — v0.1 MVP (current master)

- Pydantic Task model with idempotency key, dependencies, budget constraints and acceptance criteria.
- SQLite persistence, SQLAlchemy/Alembic, state machine, single-Worker Scheduler.
- MockWorker / LocalCodeWorker, rules-based verifier, event Journal, CLI and FastAPI.
- Historical test-evidence document reports 13 passing tests.
- Not yet production-safe for concurrent workers, external credentials or unsafe side effects.

## Milestones (outcome-based, not date-driven)

| Milestone | Outcome / demo | Linked Issues | Exit gate |
| --- | --- | --- | --- |
| **v0.2 — Delivery + Entry** | Grok Bot calls MAX to create/run/get a safe test Task; engineering changes flow via Issue → PR → CI | #2, #5 | PR CI passes 3.12 + 3.13; linkage is visible; Grok Bot receives Task ID and verified result |
| **v0.3 — Durable execution** | Kill a Worker, recover its leased Task without duplicate external effects | #3 | concurrency / expiry / crash-recovery integration tests; clear idempotency policy |
| **v0.4 — Real coding Worker** | Independently authorized CLI Worker makes a change inside an isolated worktree, verified by tests | #4 | no secret exposure, timeout/cancel respected, artifacts archived |
| **v0.5 — Orchestration + Economics** | Task DAG, approval gate, AER traces, measurable cost per verified task | #6, #7 | trace across issue/task/attempt/pr; budget policy and DAG recovery tests |
| **v1.0 — Portable runtime** | Versioned, restorable deployment with Postgres and staged release | #8 | rollback/recovery drill, documented SLO and operational runbook |

## Iteration principles

1. Implement one vertical task completion path before adding more Agent types.
2. Keep TaskService authoritative; workers cannot decide final success.
3. Prevent double execution with leases/fencing and idempotency; do not claim exactly-once remote effects.
4. Grok Bot is **not** required for persistence, replay, or recovery.
5. Treat external paid tokens, Grok Bot quotas, and compute time as separate cost sources.
6. Feature work must include tests, acceptance evidence, security note, and a linked Issue.

## Planned architecture boundary

- **Entry:** Grok Bot Skill / CLI adapter; later MCP or HTTP integration.
- **Control:** TaskService, Scheduler, DAG, approval/policy, Worker capability registry.
- **Execution:** replaceable Workers (Mock/LocalCode first; real CLI after sandbox work).
- **Evidence:** Journal, Verification, AER export, trace/cost telemetry.
- **Storage:** SQLite for local prototype, Postgres for concurrent runtime.

## Prioritization rationale

Get the end-to-end Grok Bot demonstration early (v0.2); stabilize execution before handing secrets and remote write permissions to real workers (v0.3); then introduce expensive model routing only when measurable outcomes exist. A UI/dashboard is optional until the task and evidence APIs stabilize.

## Explicit non-goals

- Re-creating Grok Bot's desktop, chat or computer-use interface.
- Editing the closed-source Grok Bot runtime or assuming undocumented hooks.
- Autonomous production deploy, payment or permissions changes.
- Multi-agent swarm orchestration before a reliable single-worker kernel exists.

## GitHub delivery protocol

See [CONTRIBUTING.md](../CONTRIBUTING.md) for Issue → Branch → PR → CI → Merge → Tag. This document is direction, not a substitute for scoped Issues. Parent epic: #1.
