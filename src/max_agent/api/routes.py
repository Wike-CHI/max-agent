from __future__ import annotations

import json
import os

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ValidationError

from max_agent.db.session import session_scope
from max_agent.entry.contract import envelope
from max_agent.entry.webhook import WebhookError, accept_inbound, verify_signature
from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.models.enums import TaskStatus
from max_agent.models.schemas import (
    ApproveRequest,
    InboundEvent,
    ScheduleRequest,
    TaskCreate,
    TaskEventRead,
    TaskRead,
)

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
    # Scheduler opens its own short transactions. Do not hold a session across worker.run().
    try:
        return Scheduler().run_one(task_id, body)
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


@router.get("/tasks/{task_id}/status")
def task_status(task_id: str) -> dict:
    """On-demand machine envelope. Does not run the worker."""
    try:
        with session_scope() as session:
            service = TaskService(session)
            task = service.get(task_id)
            events = service.list_events(task_id)
        return envelope(command="status", task=task, events=events)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/approve")
def approve_task(task_id: str, body: ApproveRequest | None = None) -> dict:
    body = body or ApproveRequest()
    try:
        with session_scope() as session:
            task = TaskService(session).approve(task_id, actor=body.actor, note=body.note)
        return envelope(command="approve", task=task)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/inbound/webhook")
async def inbound_webhook(request: Request) -> dict:
    """Signed Routine/webhook create. Returns task_id. Does not execute the task."""
    raw = await request.body()
    secret = os.environ.get("MAX_AGENT_WEBHOOK_SECRET", "")
    try:
        verify_signature(
            secret=secret,
            timestamp=request.headers.get("X-Max-Timestamp", ""),
            body=raw,
            signature=request.headers.get("X-Max-Signature", ""),
        )
        delivery_id = request.headers.get("X-Max-Delivery-Id", "").strip()
        if not delivery_id or len(delivery_id) > 128:
            raise WebhookError(400, "missing delivery id")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WebhookError(400, "invalid json") from None
        try:
            event = InboundEvent.model_validate(payload)
        except ValidationError as exc:
            raise WebhookError(400, "invalid payload") from exc
        with session_scope() as session:
            return accept_inbound(session, delivery_id=delivery_id, body=raw, event=event)
    except WebhookError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/tasks/{task_id}/events", response_model=list[TaskEventRead])
def list_events(task_id: str, limit: int = Query(default=200, ge=1, le=1000)) -> list[TaskEventRead]:
    try:
        with session_scope() as session:
            return TaskService(session).list_events(task_id, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
