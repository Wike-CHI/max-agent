"""max-agent CLI."""

from __future__ import annotations

import json
from enum import Enum
from typing import Optional

import typer

from max_agent.db.session import init_db, session_scope
from max_agent.entry.contract import envelope
from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.models.enums import TaskStatus, WorkerKind
from max_agent.models.schemas import BudgetConstraints, ScheduleRequest, TaskCreate, TaskEventRead, TaskRead

app = typer.Typer(name="max-agent", help="MAX-Agent task scheduler kernel CLI", no_args_is_help=True)


class WorkerKindOpt(str, Enum):
    mock = "mock"
    local_code = "local_code"


def _print(obj: object) -> None:
    if hasattr(obj, "model_dump"):
        typer.echo(json.dumps(obj.model_dump(mode="json"), indent=2, ensure_ascii=False))
    elif isinstance(obj, list):
        typer.echo(
            json.dumps(
                [x.model_dump(mode="json") if hasattr(x, "model_dump") else x for x in obj],
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        typer.echo(str(obj))


def _json_mode(ctx: typer.Context) -> bool:
    return bool(ctx.obj and ctx.obj.get("json"))


def _echo_json(payload: dict) -> None:
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))


def _fail(ctx: typer.Context, command: str, message: str) -> None:
    if _json_mode(ctx):
        _echo_json(envelope(command=command, ok=False, error=message))
    else:
        typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _load_events(task_id: str) -> list[TaskEventRead]:
    with session_scope() as session:
        return TaskService(session).list_events(task_id)


def _emit_task(ctx: typer.Context, command: str, task: TaskRead, *, include_events: bool = False) -> None:
    if not _json_mode(ctx):
        _print(task)
        return
    events = _load_events(task.id) if include_events else None
    _echo_json(envelope(command=command, task=task, events=events))


@app.callback()
def _root(
    ctx: typer.Context,
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Stable machine envelope. Place before the subcommand: max-agent --json create ...",
    ),
) -> None:
    """Grok callers should pass --json and then read task_id. Do not poll in a loop."""
    init_db()
    ctx.ensure_object(dict)
    ctx.obj["json"] = json_output


@app.command("init-db")
def init_db_cmd(ctx: typer.Context) -> None:
    """Create SQLite tables."""
    init_db()
    if _json_mode(ctx):
        _echo_json(envelope(command="init-db"))
        return
    typer.echo("database initialized")


@app.command("create")
def create_cmd(
    ctx: typer.Context,
    goal: str = typer.Argument(..., help="Task goal"),
    criterion: list[str] = typer.Option([], "--criterion", "-c", help="Acceptance criterion (repeatable)"),
    priority: int = typer.Option(100, "--priority", "-p"),
    worker: WorkerKindOpt = typer.Option(WorkerKindOpt.mock, "--worker", "-w"),
    payload: Optional[str] = typer.Option(None, "--payload", help="JSON worker payload"),
    idempotency_key: Optional[str] = typer.Option(None, "--idempotency-key", "-k"),
    max_attempts: int = typer.Option(3, "--max-attempts"),
    timeout: int = typer.Option(300, "--timeout"),
    side_effects: bool = typer.Option(False, "--side-effects"),
    tag: list[str] = typer.Option([], "--tag", "-t"),
) -> None:
    """Create a task and return immediately. Does not run the worker."""
    wp: dict = {}
    if payload:
        try:
            wp = json.loads(payload)
        except json.JSONDecodeError as exc:
            _fail(ctx, "create", f"invalid payload json: {exc}")
    data = TaskCreate(
        goal=goal,
        acceptance_criteria=list(criterion),
        priority=priority,
        worker_kind=WorkerKind(worker.value),
        worker_payload=wp,
        idempotency_key=idempotency_key,
        budget=BudgetConstraints(max_attempts=max_attempts, timeout_seconds=timeout),
        has_side_effects=side_effects,
        tags=list(tag),
    )
    with session_scope() as session:
        task = TaskService(session).create(data)
    _emit_task(ctx, "create", task)


def _show(ctx: typer.Context, task_id: str, command: str) -> None:
    try:
        with session_scope() as session:
            task = TaskService(session).get(task_id)
    except KeyError as exc:
        _fail(ctx, command, str(exc))
    _emit_task(ctx, command, task, include_events=True)


@app.command("get")
def get_cmd(ctx: typer.Context, task_id: str) -> None:
    """Read a task. Same data as status."""
    _show(ctx, task_id, "get")


@app.command("status")
def status_cmd(ctx: typer.Context, task_id: str) -> None:
    """On-demand status plus the latest verification. Preferred name for Grok."""
    _show(ctx, task_id, "status")


@app.command("list")
def list_cmd(
    ctx: typer.Context,
    status: Optional[str] = typer.Option(None, "--status", "-s"),
    limit: int = typer.Option(50, "--limit", "-n"),
) -> None:
    try:
        st = TaskStatus(status) if status else None
    except ValueError as exc:
        _fail(ctx, "list", str(exc))
    with session_scope() as session:
        tasks = TaskService(session).list_tasks(status=st, limit=limit)
    if _json_mode(ctx):
        payload = envelope(command="list")
        payload["tasks"] = [task.model_dump(mode="json") for task in tasks]
        _echo_json(payload)
        return
    _print(tasks)


@app.command("run")
def run_cmd(
    ctx: typer.Context,
    task_id: str = typer.Argument(..., help="Task id to schedule/run"),
    worker: Optional[WorkerKindOpt] = typer.Option(None, "--worker", "-w"),
    confirm_side_effects: bool = typer.Option(False, "--confirm-side-effects"),
) -> None:
    """Worker entry: run one attempt. Not a poll loop for the Grok caller."""
    req = ScheduleRequest(
        worker_kind=WorkerKind(worker.value) if worker else None,
        confirm_side_effects=confirm_side_effects,
    )
    try:
        task = Scheduler().run_one(task_id, req)
    except (KeyError, ValueError) as exc:
        _fail(ctx, "run", str(exc))
    _emit_task(ctx, "run", task, include_events=True)


@app.command("recover")
def recover_cmd(ctx: typer.Context) -> None:
    """Requeue or block tasks whose lease expired. Use after a process restart."""
    tasks = Scheduler().recover_expired()
    if _json_mode(ctx):
        payload = envelope(command="recover")
        payload["tasks"] = [task.model_dump(mode="json") for task in tasks]
        _echo_json(payload)
        return
    _print(tasks)


@app.command("cancel")
def cancel_cmd(ctx: typer.Context, task_id: str) -> None:
    try:
        with session_scope() as session:
            task = TaskService(session).cancel(task_id)
    except (KeyError, ValueError) as exc:
        _fail(ctx, "cancel", str(exc))
    _emit_task(ctx, "cancel", task)


@app.command("approve")
def approve_cmd(
    ctx: typer.Context,
    task_id: str,
    by: str = typer.Option("user", "--by", help="Who approved the sensitive action"),
    note: str = typer.Option("", "--note", help="Why this remote write, publish, or payment may proceed"),
) -> None:
    """Record explicit approval. Required before publish, payment, or remote write."""
    try:
        with session_scope() as session:
            task = TaskService(session).approve(task_id, actor=by, note=note)
    except (KeyError, ValueError) as exc:
        _fail(ctx, "approve", str(exc))
    _emit_task(ctx, "approve", task)


@app.command("events")
def events_cmd(
    ctx: typer.Context,
    task_id: str,
    limit: int = typer.Option(200, "--limit", "-n"),
) -> None:
    try:
        with session_scope() as session:
            service = TaskService(session)
            task = service.get(task_id)
            events = service.list_events(task_id, limit=limit)
    except KeyError as exc:
        _fail(ctx, "events", str(exc))
    if _json_mode(ctx):
        _echo_json(envelope(command="events", task=task, events=events))
        return
    _print(events)


@app.command("reopen")
def reopen_cmd(ctx: typer.Context, task_id: str) -> None:
    try:
        with session_scope() as session:
            task = TaskService(session).reopen(task_id)
    except (KeyError, ValueError) as exc:
        _fail(ctx, "reopen", str(exc))
    _emit_task(ctx, "reopen", task)


@app.command("serve")
def serve_cmd(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8080, "--port"),
) -> None:
    """Start HTTP API (uvicorn)."""
    import uvicorn

    init_db()
    uvicorn.run("max_agent.api.app:app", host=host, port=port, reload=False)


@app.command("mcp")
def mcp_cmd() -> None:
    """Stdio MCP facade (initialize, tools/list, tools/call). Does not poll."""
    from max_agent.mcp.server import serve_stdio

    serve_stdio()


if __name__ == "__main__":
    app()
