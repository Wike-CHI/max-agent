# Read-only MAX console

## Scope

The first version is a private query console for humans and agents sharing the
same Task DB. It deliberately has no task execution or approval controls. The
FastAPI application serves a React/Vite build on the same origin at `/console/`.
There is no new database, background scheduler, remote MCP service, or production
deployment.

For the optional free edge-hosted console and Git-based updates, see
[Cloudflare deployment](cloudflare.md). That deployment serves the UI and a
read-only proxy; the Python API and Task DB still need their own reachable host.

The implementation is being prepared locally. Before publishing it, link a scoped
GitHub Issue and follow the repository's draft PR, CI, review, and release protocol.
This document is not authorization to push, merge, publish, or deploy.

## Run and package

1. Use Node.js 24 and run `npm ci && npm test && npm run build` in `frontend/`.
2. Set an explicit `MAX_AGENT_DATABASE_URL` (or absolute `MAX_AGENT_DB_PATH`) in
   API, MCP, and worker processes. All three must point at the same database.
3. Set `MAX_AGENT_API_ACTOR` to the same owner in API and MCP. The default is `user`.
4. Set a strong `MAX_AGENT_API_TOKEN` securely in the API process; do not commit it,
   add it to a URL, or include it in a frontend environment variable.
5. Run migrations before starting API/worker, then run
   `max-agent serve --host 127.0.0.1 --port 8080` and open
   `http://127.0.0.1:8080/console/`.
6. If execution is wanted, start `max-agent worker` separately using the existing
   [worker runbook](operations/worker.md). Viewing or refreshing the page never
   starts it.

The UI build lives in `src/max_agent/web/dist/`. After building it, run `uv build`
and `uv run python scripts/check_console_package.py`. CI and release verification
build and test the frontend before checking the wheel. A Python source install
without a UI build still runs the API and returns a helpful 503 at `/console/`.
Restart the API after creating a previously missing build.

For frontend development, run the API on port 8080, then `npm run dev` in
`frontend/`. Vite listens on loopback and proxies `/v1` to that API. The production
build is served by FastAPI and does not require a separate Node.js process.

## Authentication and identity

The console asks for the configured bearer token and keeps it in JavaScript
memory only. It does not save it in local/session storage, cookies, the URL, or a
frontend build. Reloading or disconnecting requires reconnecting. Responses are
marked `no-store`, the console has a same-origin content policy, and external
links do not send the page's referrer.

The console only calls GET endpoints. **The bearer token itself still permits
the existing write API.** This version does not introduce read-only credential
scopes, browser session authentication, or multi-tenant IAM. Use loopback or a
private tunnel. Remote transport requires TLS or a private encrypted tunnel;
do not expose the bare service publicly.

HTTP and MCP use the configured owner for task creation and queries. MCP rejects
access to another owner's task and rejects a spoofed approval actor. Local stdio
MCP is trusted local access, not a network authentication boundary. Existing rows
are not reassigned when `MAX_AGENT_API_ACTOR` changes. A different owner can
correctly see an empty list while another owner's tasks still exist.

Legacy CLI commands remain a trusted local administrative interface. CLI-created
tasks default to `user` and its reads are unscoped. Use MCP or HTTP for scoped
agent access, especially with a non-default owner. Do not change an actor merely
to bypass ownership checks.

## Shared read query contracts

- `GET /v1/tasks/query`: `q` (literal substring in task ID, goal, or issue reference;
  maximum 256 characters), optional `status`, `limit` (1–100, default 25), and
  optional opaque `cursor`. Returns `items`, `total`, and `next_cursor`.
- Task ordering is descending creation time, then ID. `total` means all matches
  for the current owner and filter, not the number on the current page. Cursors
  are bound to owner and filter and rejected if reused for a different query.
  They are positions, not database snapshots; concurrent updates may change the
  matching total. Refresh starts again from the newest page.
- `GET /v1/tasks/{id}/events/query`: `limit` (1–200, default 50) and optional
  `cursor`. The initial page contains the latest events in chronological order.
  `next_cursor` requests the next older page, which should be prepended. Returns
  `items`, `next_cursor`, and `has_more`.
- `GET /v1/tasks/{id}/status`: the stable machine envelope. `verification` is
  queried independently across the full journal, not inferred from a truncated
  event page.
- `GET /v1/tasks/{id}/trace`: recorded attempts, costs, verification evidence,
  references, and AER export state.

All require the existing bearer authentication and owner authorization. Invalid
query values/cursors produce 422, missing tasks 404, and other-owner tasks 403.
The old `/v1/tasks` and `/events` list contracts remain available. The legacy
events list still returns its first events up to the limit; use `/events/query`
for current progress and pagination. `/status.events` likewise remains a legacy
window; its separate `verification` is current.

MCP exposes `max_list_tasks` (task query fields), `max_events_query` (`task_id`,
`limit`, `cursor`), and `max_trace` (`task_id`) as read-only tools. Their successful
JSON results mirror the corresponding HTTP page or trace. Existing MCP commands
retain their envelopes. `max_status.verification` and CLI JSON verification also
read the actual latest verification. No query executes a worker.

## What the console can honestly show

- Task lifecycle and recorded lease information, not a real-time agent-presence
  system. A valid lease is not proof that a worker process is currently alive.
- `mock`, `local_code`, and `cli` are distinct worker types. A CLI task alone is
  not proof of a real model call; worker output must record
  `real_cli_invoked=true`. Missing evidence stays unconfirmed. Stub success is
  not real-CLI acceptance. See [worker requirements](operations/worker.md).
- Cost markers remain `measured`, `estimate`, or `unknown`. Unknown cost is never
  replaced with zero, and unrelated ledgers/currencies are not fabricated into
  a monetary total.
- Issue/PR/artifact references are recorded evidence, not live GitHub checks.
  Local artifact paths are text, not downloadable browser URLs. Only HTTP(S)
  references can become external links.
- `/v1/telemetry` is process-local API telemetry, not worker fleet health. The
  console does not advertise agent-online counts from it.
- No percentage-progress estimate, automatic model execution, public sharing,
  production deployment, or remote approval is implied by this UI.

## Validation

Run the full Python suite, frontend tests/build, package asset check, and a browser
smoke test against an isolated fixture database. Cover connection errors,
authentication expiry, filters, pagination, long journals, latest verification,
detail navigation, disconnect/reconnect, and small screens. Fixture data must be
clearly distinguished from a real MAX deployment; never inject it into the user's
actual task database.
