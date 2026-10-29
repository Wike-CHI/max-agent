from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from max_agent.db.session import session_scope
from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.models.enums import TaskStatus
from max_agent.models.schemas import ScheduleRequest, TaskCreate, TaskEventRead, TaskRead

router = APIRouter(prefix="/v1")


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "max-agent"}


@router.post("/tasks", response_model=TaskRead)
def create_task(body: TaskCreate) -> TaskRead:
    with session_scope() as session:
        return TaskService(session).create(body)


@router.get("/tasks", response_model=list[TaskRead])
def list_tasks(
    status: TaskStatus | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[TaskRead]:
    with session_scope() as session:
        return TaskService(session).list_tasks(status=status, limit=limit)


@router.get("/tasks/{task_id}", response_model=TaskRead)
def get_task(task_id: str) -> TaskRead:
    try:
        with session_scope() as session:
            return TaskService(session).get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/schedule", response_model=TaskRead)
def schedule_task(task_id: str, body: ScheduleRequest | None = None) -> TaskRead:
    try:
        with session_scope() as session:
            return Scheduler(session).run_one(task_id, body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/cancel", response_model=TaskRead)
def cancel_task(task_id: str) -> TaskRead:
    try:
        with session_scope() as session:
            return TaskService(session).cancel(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/tasks/{task_id}/events", response_model=list[TaskEventRead])
def list_events(task_id: str, limit: int = Query(default=200, ge=1, le=1000)) -> list[TaskEventRead]:
    try:
        with session_scope() as session:
            return TaskService(session).list_events(task_id, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
