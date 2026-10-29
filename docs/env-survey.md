# Phase 0 — Environment Survey

Surveyed on: 2026-10-29 17:09 CST (Asia/Shanghai)

## Host

| Item | Result |
|------|--------|
| OS | Debian GNU/Linux 13 (trixie), kernel 6.12.94+, x86_64 |
| Hostname | grok-bot-vm-309395684 |
| cwd | `/workspace` |
| Project dir | `/workspace/max-agent` (created) |

## Runtimes

| Tool | Version | Status |
|------|---------|--------|
| Python | 3.13.5 (`/usr/bin/python3`) | Available (meets 3.12+) |
| python3.12 | — | Not installed (3.13 OK) |
| Node.js | v20.19.2 | Available |
| npm | 11.21.1 | Available |
| uv | 0.13.5 | Available (preferred package manager) |
| pip | 25.1.1 | Available |

## Dev / Ops CLIs

| Tool | Status |
|------|--------|
| Git | 2.47.3 — Available |
| gh | Available, authenticated as `Wike-CHI` (scopes: gist, read:org, repo) |
| Docker | **Missing** (`command not found`) |
| curl / wget / jq | Available |
| Network (PyPI) | Reachable (HTTP/2 200) |

## Coding / Cloud Agents

| Capability | Status |
|------------|--------|
| `claude` / `codex` / `aider` CLIs | Not on PATH |
| Cursor CloudAgent as subprocess CLI | Not found as standalone binary |
| `cursor-github` / `cursor-origin` MCP | Present in dynamic tool catalog (parent/orchestrator use) |
| Paid LLM APIs | Not configured; do not use without authorization |

## Implications for MVP

- Use **Python 3.13 + uv** for packaging and venv.
- Use **SQLite** for local persistence (no Docker → no local Postgres container).
- Worker integration: implement **Mock Worker** + a safe **LocalCodeWorker** (runs local shell/pytest only). Do **not** call paid APIs.
- CloudAgent delegation exists at Grok Bot orchestrator layer; MAX-Agent kernel must not depend on undocumented host SDKs.
- Git: local commits only; **no remote push** without explicit authorization.
