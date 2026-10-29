"""max-agent CLI."""

from __future__ import annotations

import json
from enum import Enum
from typing import Optional

import typer

from max_agent.db.session import init_db, session_scope
from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.models.enums import TaskStatus, WorkerKind
from max_agent.models.schemas import BudgetConstraints, ScheduleRequest, TaskCreate

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


@app.callback()
def _root(ctx: typer.Context) -> None:
    init_db()


@app.command("init-db")
def init_db_cmd() -> None:
    """Create SQLite tables."""
    init_db()
    typer.echo("database initialized")


@app.command("create")
def create_cmd(
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
    """Create a task (idempotent when --idempotency-key set)."""
    wp = json.loads(payload) if payload else {}
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
    _print(task)


@app.command("get")
def get_cmd(task_id: str) -> None:
    with session_scope() as session:
        task = TaskService(session).get(task_id)
    _print(task)


@app.command("list")
def list_cmd(
    status: Optional[str] = typer.Option(None, "--status", "-s"),
    limit: int = typer.Option(50, "--limit", "-n"),
) -> None:
    st = TaskStatus(status) if status else None
    with session_scope() as session:
        tasks = TaskService(session).list_tasks(status=st, limit=limit)
    _print(tasks)


@app.command("run")
def run_cmd(
    task_id: str = typer.Argument(..., help="Task id to schedule/run"),
    worker: Optional[WorkerKindOpt] = typer.Option(None, "--worker", "-w"),
    confirm_side_effects: bool = typer.Option(False, "--confirm-side-effects"),
) -> None:
    """Schedule and run a task with the worker (single-worker)."""
    req = ScheduleRequest(
        worker_kind=WorkerKind(worker.value) if worker else None,
        confirm_side_effects=confirm_side_effects,
    )
    with session_scope() as session:
        task = Scheduler(session).run_one(task_id, req)
    _print(task)


@app.command("cancel")
def cancel_cmd(task_id: str) -> None:
    with session_scope() as session:
        task = TaskService(session).cancel(task_id)
    _print(task)


@app.command("events")
def events_cmd(task_id: str, limit: int = typer.Option(200, "--limit", "-n")) -> None:
    with session_scope() as session:
        events = TaskService(session).list_events(task_id, limit=limit)
    _print(events)


@app.command("reopen")
def reopen_cmd(task_id: str) -> None:
    with session_scope() as session:
        task = TaskService(session).reopen(task_id)
    _print(task)


@app.command("serve")
def serve_cmd(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8080, "--port"),
) -> None:
    """Start HTTP API (uvicorn)."""
    import uvicorn

    init_db()
    uvicorn.run("max_agent.api.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    app()
