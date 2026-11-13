from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError

from max_agent.api.auth import (
    authorize_task,
    require_actor_match,
    require_any_token,
    require_scope,
)
from max_agent.api.scopes import (
    APPROVE,
    CANCEL,
    CREATE,
    DISPATCH,
    EXECUTE_INLINE,
    OPS,
    READ,
)
from max_agent.api.token_registry import AuthContext
from max_agent.db.session import session_scope
from max_agent.ops.health import database_ready, liveness
from max_agent.ops.telemetry import get_telemetry
from max_agent.entry.contract import envelope
from max_agent.entry.webhook import WebhookError, accept_inbound, verify_signature
from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.kernel.query import TaskQuery
from max_agent.models.aer import EconomicsReport, ExportRetryResult, TaskTrace
from max_agent.models.enums import TaskStatus
from max_agent.models.schemas import (
    ApproveRequest,
    DispatchRequest,
    InboundEvent,
    RejectRequest,
    ScheduleRequest,
    TaskCreate,
    TaskEventRead,
    TaskGraphCreate,
    TaskRead,
    TaskPage,
    TaskEventPage,
)

router = APIRouter(prefix="/v1")


@router.get("/health")
def health() -> dict[str, str]:
    return liveness()


@router.get("/ready")
def ready() -> dict[str, str]:
    try:
        database_ready()
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable") from None
    return {"status": "ready", "database": "ok"}


@router.get("/auth/whoami")
def whoami(context: AuthContext = Depends(require_any_token)) -> dict:
    """返回调用者身份与 scope 集合；不要求 read，不返回任何秘密或哈希。"""
    return {
        "token_id": context.token_id,
        "principal": context.principal,
        "scopes": sorted(context.scopes),
    }


@router.get("/telemetry")
def telemetry(context: AuthContext = Depends(require_scope(READ))) -> dict:
    """In-process counters and recent spans. Not the economics report at /metrics."""
    return get_telemetry().snapshot()


@router.post("/tasks", response_model=TaskRead)
def create_task(body: TaskCreate, context: AuthContext = Depends(require_scope(CREATE))) -> TaskRead:
    try:
        with session_scope() as session:
            return TaskService(session).create(body, created_by=context.principal)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/graph", response_model=list[TaskRead])
def create_task_graph(
    body: TaskGraphCreate,
    context: AuthContext = Depends(require_scope(CREATE)),
) -> list[TaskRead]:
    try:
        with session_scope() as session:
            return TaskService(session).create_graph(body.nodes, created_by=context.principal)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/tasks", response_model=list[TaskRead])
def list_tasks(
    status: TaskStatus | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    context: AuthContext = Depends(require_scope(READ)),
) -> list[TaskRead]:
    with session_scope() as session:
        return TaskService(session).list_tasks(status=status, limit=limit, created_by=context.principal)


@router.get("/tasks/query", response_model=TaskPage)
def query_tasks(
    q: str = Query(default="", max_length=256),
    status: TaskStatus | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=1024),
    context: AuthContext = Depends(require_scope(READ)),
) -> TaskPage:
    try:
        with session_scope() as session:
            return TaskQuery(session, context.principal).tasks(q=q, status=status, limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/tasks/{task_id}/events/query", response_model=TaskEventPage)
def query_events(
    task_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1024),
    context: AuthContext = Depends(require_scope(READ)),
) -> TaskEventPage:
    try:
        with session_scope() as session:
            authorize_task(session, task_id, context.principal)
            return TaskQuery(session, context.principal).events(task_id, limit=limit, cursor=cursor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/tasks/{task_id}", response_model=TaskRead)
def get_task(task_id: str, context: AuthContext = Depends(require_scope(READ))) -> TaskRead:
    try:
        with session_scope() as session:
            service = TaskService(session)
            authorize_task(session, task_id, context.principal)
            return service.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/queue/run", response_model=list[TaskRead])
def run_queue(
    limit: int = Query(default=50, ge=1, le=500),
    context: AuthContext = Depends(require_scope(EXECUTE_INLINE)),
) -> list[TaskRead]:
    """Run ready tasks in fair order. Does not start work past the concurrency cap."""
    return Scheduler(created_by=context.principal).run_queue(max_tasks=limit)


@router.post("/tasks/{task_id}/schedule", response_model=TaskRead)
def schedule_task(
    task_id: str,
    body: ScheduleRequest | None = None,
    context: AuthContext = Depends(require_scope(EXECUTE_INLINE)),
) -> TaskRead:
    # Scheduler opens its own short transactions. Do not hold a session across worker.run().
    try:
        with session_scope() as session:
            authorize_task(session, task_id, context.principal)
        return Scheduler(created_by=context.principal).run_one(task_id, body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/cancel", response_model=TaskRead)
def cancel_task(task_id: str, context: AuthContext = Depends(require_scope(CANCEL))) -> TaskRead:
    try:
        with session_scope() as session:
            authorize_task(session, task_id, context.principal)
            return TaskService(session).cancel(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/tasks/{task_id}/status")
def task_status(task_id: str, context: AuthContext = Depends(require_scope(READ))) -> dict:
    """On-demand machine envelope. Does not run the worker."""
    try:
        with session_scope() as session:
            service = TaskService(session)
            authorize_task(session, task_id, context.principal)
            task = service.get(task_id)
            events = service.list_events(task_id)
            verification = service.latest_verification(task_id)
        return envelope(command="status", task=task, events=events, verification=verification)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/approve")
def approve_task(
    task_id: str,
    body: ApproveRequest | None = None,
    context: AuthContext = Depends(require_scope(APPROVE)),
) -> dict:
    body = body or ApproveRequest()
    require_actor_match(context.principal, body.actor)
    try:
        with session_scope() as session:
            authorize_task(session, task_id, context.principal)
            task = TaskService(session).approve(
                task_id,
                actor=context.principal,
                note=body.note,
                scope=list(body.scope),
                ttl_seconds=body.ttl_seconds,
            )
        return envelope(command="approve", task=task)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/reject")
def reject_task(
    task_id: str,
    body: RejectRequest | None = None,
    context: AuthContext = Depends(require_scope(APPROVE)),
) -> dict:
    body = body or RejectRequest()
    require_actor_match(context.principal, body.actor)
    try:
        with session_scope() as session:
            authorize_task(session, task_id, context.principal)
            task = TaskService(session).reject(task_id, actor=context.principal, note=body.note)
        return envelope(command="reject", task=task)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/dispatch")
def dispatch_task(
    task_id: str,
    body: DispatchRequest | None = None,
    context: AuthContext = Depends(require_scope(DISPATCH)),
) -> dict:
    """Hand an approved task over to its worker.

    The full dispatch gate (version / content-hash checks, lease hand-off) is a
    later task. Here we authenticate with the dispatch scope and resolve the
    task: an unknown task is a 404.
    """
    try:
        with session_scope() as session:
            service = TaskService(session)
            authorize_task(session, task_id, context.principal)
            task = service.get(task_id)
            dispatch = getattr(service, "dispatch", None)
            if dispatch is None:
                raise HTTPException(status_code=501, detail="dispatch is not implemented yet")
            task = dispatch(task_id) or task
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return envelope(command="dispatch", task=task)


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


@router.get("/tasks/{task_id}/trace", response_model=TaskTrace)
def task_trace(task_id: str, context: AuthContext = Depends(require_scope(READ))) -> TaskTrace:
    try:
        with session_scope() as session:
            authorize_task(session, task_id, context.principal)
            return TaskService(session).trace(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/metrics", response_model=EconomicsReport)
def metrics(context: AuthContext = Depends(require_scope(READ))) -> EconomicsReport:
    with session_scope() as session:
        return TaskService(session).metrics(created_by=context.principal)


@router.post("/aer/retry", response_model=ExportRetryResult)
def retry_aer_exports(context: AuthContext = Depends(require_scope(OPS))) -> ExportRetryResult:
    """Redeliver pending and failed AER rows. Does not run tasks."""
    with session_scope() as session:
        return TaskService(session).retry_aer_exports()


@router.get("/tasks/{task_id}/events", response_model=list[TaskEventRead])
def list_events(
    task_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    context: AuthContext = Depends(require_scope(READ)),
) -> list[TaskEventRead]:
    try:
        with session_scope() as session:
            authorize_task(session, task_id, context.principal)
            return TaskService(session).list_events(task_id, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
