"""Bearer authentication and single-user task authorization for the HTTP API."""

from __future__ import annotations

import hmac
import os
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from max_agent.db.models import TaskRow

_bearer = HTTPBearer(auto_error=False)


def require_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    configured = os.environ.get("MAX_AGENT_API_TOKEN", "")
    presented = credentials.credentials if credentials is not None else ""
    if not configured or not presented or not hmac.compare_digest(configured.encode(), presented.encode()):
        raise HTTPException(
            status_code=401,
            detail="valid bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return os.environ.get("MAX_AGENT_API_ACTOR", "user").strip()[:128] or "user"


def require_actor_match(principal: str, requested: str | None) -> None:
    if requested is not None and requested.strip() != principal:
        raise HTTPException(status_code=403, detail="actor must match authenticated principal")


def authorize_task(session: Session, task_id: str, principal: str) -> TaskRow:
    row = session.get(TaskRow, task_id)
    if row is None:
        raise KeyError(f"task not found: {task_id}")
    authorize_task_row(row, principal)
    return row


def authorize_task_row(row: TaskRow, principal: str) -> None:
    if row.created_by != principal:
        raise HTTPException(status_code=403, detail="task access denied")
