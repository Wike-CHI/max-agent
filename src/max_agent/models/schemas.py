"""Pydantic v2 API / domain schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from max_agent.models.enums import EventType, TaskStatus, WorkerKind


class BudgetConstraints(BaseModel):
    """Soft budget limits; enforcement is policy-level in MVP."""

    max_attempts: int = Field(default=3, ge=1, le=100)
    timeout_seconds: int = Field(default=300, ge=1, le=86400)
    max_cost_units: float = Field(default=0.0, ge=0.0)
    allow_auto_retry_side_effects: bool = Field(
        default=False,
        description="If true, tasks marked has_side_effects may auto-retry without confirmation.",
    )


class TaskCreate(BaseModel):
    goal: str = Field(..., min_length=1, max_length=4000)
    acceptance_criteria: list[str] = Field(default_factory=list)
    priority: int = Field(default=100, ge=0, le=1000)
    dependencies: list[str] = Field(default_factory=list)
    timeout_seconds: int | None = None
    budget: BudgetConstraints = Field(default_factory=BudgetConstraints)
    idempotency_key: str | None = Field(default=None, max_length=128)
    worker_kind: WorkerKind = WorkerKind.MOCK
    worker_payload: dict[str, Any] = Field(default_factory=dict)
    has_side_effects: bool = False
    tags: list[str] = Field(default_factory=list)

    @field_validator("acceptance_criteria")
    @classmethod
    def _nonempty_criteria_items(cls, v: list[str]) -> list[str]:
        return [c.strip() for c in v if c and c.strip()]


class TaskRead(BaseModel):
    id: str
    goal: str
    acceptance_criteria: list[str]
    priority: int
    dependencies: list[str]
    status: TaskStatus
    attempts: int
    max_attempts: int
    timeout_seconds: int
    budget: BudgetConstraints
    idempotency_key: str | None
    worker_kind: WorkerKind
    worker_payload: dict[str, Any]
    has_side_effects: bool
    tags: list[str]
    result_summary: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    leased_by: str | None = None
    lease_expires_at: datetime | None = None

    model_config = {"from_attributes": True}


class TaskEventRead(BaseModel):
    id: int
    task_id: str
    event_type: EventType
    message: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ScheduleRequest(BaseModel):
    worker_kind: WorkerKind | None = None
    confirm_side_effects: bool = False
    force: bool = False


class WorkerResult(BaseModel):
    """Unified worker completion report (done ≠ success)."""

    ok: bool
    summary: str
    outputs: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    recoverable: bool = True
    error: str | None = None


class VerificationResult(BaseModel):
    passed: bool
    checks: list[dict[str, Any]] = Field(default_factory=list)
    message: str = ""
