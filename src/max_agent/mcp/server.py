"""Line-delimited JSON-RPC tools for a Grok Bot.

Supported methods: initialize, tools/list, tools/call.
Notifications are ignored. This process does not poll task status.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

from max_agent import __version__
from max_agent.db.session import session_scope
from max_agent.entry.contract import envelope
from max_agent.kernel.service import TaskService
from max_agent.models.enums import WorkerKind
from max_agent.models.schemas import TaskCreate

PROTOCOL = "2024-11-05"


def _create(arguments: dict[str, Any]) -> dict[str, Any]:
    kind = WorkerKind(arguments.get("worker_kind") or WorkerKind.MOCK.value)
    data = TaskCreate(
        goal=arguments["goal"],
        acceptance_criteria=list(arguments.get("acceptance_criteria") or []),
        priority=int(arguments.get("priority") or 100),
        worker_kind=kind,
        worker_payload=dict(arguments.get("worker_payload") or {}),
        idempotency_key=arguments.get("idempotency_key"),
        has_side_effects=bool(arguments.get("has_side_effects") or False),
        tags=list(arguments.get("tags") or []),
        dependencies=list(arguments.get("dependencies") or []),
    )
    with session_scope() as session:
        task = TaskService(session).create(data)
    return envelope(command="max_create", task=task)


def _status(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments["task_id"])
    with session_scope() as session:
        service = TaskService(session)
        task = service.get(task_id)
        events = service.list_events(task_id)
    return envelope(command="max_status", task=task, events=events)


def _events(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments["task_id"])
    limit = int(arguments.get("limit") or 200)
    with session_scope() as session:
        service = TaskService(session)
        task = service.get(task_id)
        events = service.list_events(task_id, limit=limit)
    return envelope(command="max_events", task=task, events=events)


def _cancel(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments["task_id"])
    with session_scope() as session:
        task = TaskService(session).cancel(task_id)
    return envelope(command="max_cancel", task=task)


def _approve(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments["task_id"])
    actor = str(arguments.get("actor") or "user")
    note = str(arguments.get("note") or "")
    scope = list(arguments.get("scope") or [])
    ttl = arguments.get("ttl_seconds")
    with session_scope() as session:
        task = TaskService(session).approve(
            task_id,
            actor=actor,
            note=note,
            scope=scope,
            ttl_seconds=None if ttl is None else int(ttl),
        )
    return envelope(command="max_approve", task=task)


def _reject(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments["task_id"])
    actor = str(arguments.get("actor") or "user")
    note = str(arguments.get("note") or "")
    with session_scope() as session:
        task = TaskService(session).reject(task_id, actor=actor, note=note)
    return envelope(command="max_reject", task=task)


_TOOL_FNS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "max_create": _create,
    "max_status": _status,
    "max_events": _events,
    "max_cancel": _cancel,
    "max_approve": _approve,
    "max_reject": _reject,
}


def tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "max_create",
            "description": "Create a MAX task and return task_id immediately. Does not run or poll.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    "worker_kind": {"type": "string", "enum": ["mock", "local_code", "cli"]},
                    "worker_payload": {"type": "object"},
                    "idempotency_key": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "has_side_effects": {"type": "boolean"},
                    "priority": {"type": "integer"},
                    "dependencies": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["goal"],
            },
        },
        {
            "name": "max_status",
            "description": "Read task status and the latest verification once. Does not poll.",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        },
        {
            "name": "max_events",
            "description": "Read the task journal once.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "max_cancel",
            "description": "Cancel a non-terminal task.",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        },
        {
            "name": "max_approve",
            "description": "Record explicit user approval before publish, payment, or remote write.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "actor": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "max_reject",
            "description": "Reject approval. External actions must not run after this.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "actor": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["task_id"],
            },
        },
    ]


def handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    if not isinstance(method, str):
        return _error(message.get("id"), -32600, "invalid request")
    if method.startswith("notifications/") or "id" not in message:
        return None
    msg_id = message.get("id")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _error(msg_id, -32602, "invalid params")
    if method == "initialize":
        return _result(
            msg_id,
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "max-agent", "version": __version__},
            },
        )
    if method == "tools/list":
        return _result(msg_id, {"tools": tool_specs()})
    if method == "tools/call":
        return _result(msg_id, _tool_call(params))
    return _error(msg_id, -32601, "method not found")


def serve_stdio() -> None:
    """Read one JSON-RPC object per line. Write responses to stdout only."""
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            sys.stdout.flush()
            continue
        if not isinstance(message, dict):
            sys.stdout.write(json.dumps(_error(None, -32600, "invalid request")) + "\n")
            sys.stdout.flush()
            continue
        response = handle_message(message)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _tool_call(params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name") or "")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        arguments = {}
    fn = _TOOL_FNS.get(name)
    if fn is None:
        body = envelope(command=name or "tools/call", ok=False, error=f"unknown tool: {name}")
        return _tool_result(body, is_error=True)
    try:
        body = fn(arguments)
    except (KeyError, ValueError, TypeError) as exc:
        body = envelope(command=name, ok=False, error=str(exc))
        return _tool_result(body, is_error=True)
    return _tool_result(body, is_error=not bool(body.get("ok", True)))


def _tool_result(body: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}],
        "isError": is_error,
    }


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
