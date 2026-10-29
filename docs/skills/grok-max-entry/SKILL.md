# Grok Bot skill: call MAX, then leave

MAX is the task kernel. Grok Bot is the entry. Do not store the task only in the chat, and do not keep a process alive to poll.

Safe demos use `worker_kind=mock` or `local_code`. They do not need production credentials. Do not attach a worker that can deploy, pay, or write to production.

## One-shot CLI

Put `--json` before the subcommand. Parse the object on stdout. The keys do not change between commands:

`ok`, `command`, `task_id`, `status`, `approval_required`, `approved`, `idempotent_hit`, `deduplicated`, `idempotency_key`, `verification`, `error`, `task`, `events`

```bash
max-agent --json create "<goal>" \
  -c worker_ok \
  -w mock \
  --payload '{"mode":"success","answer":"hello"}' \
  -k "<idempotency-key>"
```

`create` returns `task_id` immediately. `status` is still `ready` (or `blocked` when approval is required). `task.attempts` is `0`. That call did not run the worker.

Later, read once:

```bash
max-agent --json status <task_id>
max-agent --json events <task_id>
max-agent --json cancel <task_id>
```

`status` includes the latest `verification` object when the kernel has verified the attempt. There is no wait/poll helper. If the task is still `ready` or `running`, stop and read again only when the user asks or a later turn starts.

`run <task_id>` is the worker, not the bot. One invocation runs one attempt. Do not loop it.

Passing the same `-k` / `--idempotency-key` returns the original `task_id` and sets `idempotent_hit` to true.

Without `--json`, `create` still prints the task object with `id` for older scripts. Grok should always pass `--json`.

## Approval

Tags `delete`, `deploy`, `payment`, `publish`, `permission`, `production`, and `external_write` create the task as `blocked` with `approval_required: true`.

`run --confirm-side-effects` does not clear that gate. Ask the user, then:

```bash
max-agent --json approve <task_id> --by "<user>" --note "<why>"
```

Only after `approved` is true may a worker run that task. Mock and local_code tasks without those tags run with no extra approval.

A side-effect retry that is not in that tag set still needs `--confirm-side-effects`, an idempotency key, a budget flag, or a prior `approve`.

## MCP facade

`max-agent mcp` speaks newline-delimited JSON-RPC on stdin/stdout. Methods: `initialize`, `tools/list`, `tools/call`. Notifications are ignored.

Tools: `max_create`, `max_status`, `max_events`, `max_cancel`, `max_approve`. Each call does one kernel operation and returns the same envelope as text. The server does not poll.

This is not a full MCP host. It has no resources or prompts.

## Inbound webhook / Routine

`POST /v1/inbound/webhook` creates a task and returns. It does not execute.

Set `MAX_AGENT_WEBHOOK_SECRET` outside the repo. Headers:

- `X-Max-Timestamp`: unix seconds, within 300 seconds of the server clock
- `X-Max-Signature`: hex HMAC-SHA256 of `{timestamp}.{raw body}` (optional `sha256=` prefix)
- `X-Max-Delivery-Id`: unique per delivery

Body field `idempotency_key` is required. The same delivery id returns the original task with `deduplicated: true`. A new delivery id with the same key returns that same `task_id` with `idempotent_hit: true`. Missing or wrong secrets are rejected. The process does not start when the secret is unset.

## 中文

Grok 只负责下发和取回。`create` 马上给出 `task_id`。需要证据时再调用一次 `status`（里面有 `verification`）和 `events`。不要写轮询循环。`run` 是本地 worker 的一次执行。发布、付费、远程写必须先 `approve`。mock / local_code 演示不要配置生产密钥。
