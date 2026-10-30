"""Pydantic v2 API / domain schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from max_agent.models.enums import EventType, TaskStatus, WorkerKind


class BudgetCeilings(BaseModel):
    """Per-ledger caps. None means that ledger has no ceiling.

    `0` is a real cap. It is not a stand-in for missing data.
    """

    grok_bot_quota: float | None = Field(default=None, ge=0)
    cli_quota: float | None = Field(default=None, ge=0)
    api_spend: float | None = Field(default=None, ge=0)


class BudgetConstraints(BaseModel):
    """Budget limits. `max_cost_units == 0` means the aggregate cap is unset."""

    max_attempts: int = Field(default=3, ge=1, le=100)
    timeout_seconds: int = Field(default=300, ge=1, le=86400)
    max_cost_units: float = Field(
        default=0.0,
        ge=0.0,
        description="Aggregate cap across ledgers. 0 means unset, preserving older tasks.",
    )
    allow_auto_retry_side_effects: bool = Field(
        default=False,
        description="If true, tasks marked has_side_effects may auto-retry without confirmation.",
    )
    ceilings: BudgetCeilings = Field(default_factory=BudgetCeilings)


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
    issue_ref: str | None = Field(
        default=None,
        max_length=256,
        description="Caller-supplied issue id or URL. Never inferred from the goal text.",
    )

    @field_validator("acceptance_criteria")
    @classmethod
    def _nonempty_criteria_items(cls, v: list[str]) -> list[str]:
        return [c.strip() for c in v if c and c.strip()]


class GraphNode(BaseModel):
    """One node in a task graph. `depends_on` entries are sibling keys or existing task ids."""

    key: str = Field(..., min_length=1, max_length=128)
    goal: str = Field(..., min_length=1, max_length=4000)
    acceptance_criteria: list[str] = Field(default_factory=list)
    priority: int = Field(default=100, ge=0, le=1000)
    depends_on: list[str] = Field(default_factory=list)
    timeout_seconds: int | None = None
    budget: BudgetConstraints = Field(default_factory=BudgetConstraints)
    idempotency_key: str | None = Field(default=None, max_length=128)
    worker_kind: WorkerKind = WorkerKind.MOCK
    worker_payload: dict[str, Any] = Field(default_factory=dict)
    has_side_effects: bool = False
    tags: list[str] = Field(default_factory=list)
    issue_ref: str | None = Field(default=None, max_length=256)

    @field_validator("acceptance_criteria")
    @classmethod
    def _nonempty_node_criteria(cls, v: list[str]) -> list[str]:
        return [c.strip() for c in v if c and c.strip()]

    def to_create(self) -> TaskCreate:
        return TaskCreate(
            goal=self.goal,
            acceptance_criteria=list(self.acceptance_criteria),
            priority=self.priority,
            dependencies=[],
            timeout_seconds=self.timeout_seconds,
            budget=self.budget,
            idempotency_key=self.idempotency_key,
            worker_kind=self.worker_kind,
            worker_payload=dict(self.worker_payload),
            has_side_effects=self.has_side_effects,
            tags=list(self.tags),
            issue_ref=self.issue_ref,
        )


class TaskGraphCreate(BaseModel):
    nodes: list[GraphNode] = Field(..., min_length=1)


class TaskRead(BaseModel):
    id: str
    goal: str
    created_by: str
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
    fencing_token: int = 0
    next_retry_at: datetime | None = None
    failure_class: str | None = None
    approved_at: datetime | None = None
    approved_by: str | None = None
    approval_note: str | None = None
    approval_scope: list[str] = Field(default_factory=list)
    approval_expires_at: datetime | None = None
    approval_decision: str | None = None
    cancel_requested_at: datetime | None = None
    issue_ref: str | None = None
    idempotent_replay: bool = Field(
        default=False,
        exclude=True,
        description="Set on the create response when an idempotency key hit an existing task.",
    )

    model_config = {"from_attributes": True}


class TaskEventRead(BaseModel):
    id: int
    task_id: str
    event_type: EventType
    message: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ApproveRequest(BaseModel):
    actor: str | None = Field(default=None, max_length=128)
    note: str = Field(default="", max_length=2000)
    scope: list[str] = Field(
        default_factory=list,
        description="Permission scope. Empty means the task's sensitive tags, or 'run' when it has none.",
    )
    ttl_seconds: int | None = Field(default=None, ge=1, le=86400 * 30)


class RejectRequest(BaseModel):
    actor: str | None = Field(default=None, max_length=128)
    note: str = Field(default="", max_length=2000)


class InboundEvent(BaseModel):
    """Routine / webhook body. Associated to a task by idempotency_key."""

    idempotency_key: str = Field(..., min_length=1, max_length=128)
    goal: str = Field(..., min_length=1, max_length=4000)
    acceptance_criteria: list[str] = Field(default_factory=list)
    worker_kind: WorkerKind = WorkerKind.MOCK
    worker_payload: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    has_side_effects: bool = False
    priority: int = Field(default=100, ge=0, le=1000)


class ScheduleRequest(BaseModel):
    worker_kind: WorkerKind | None = None
    confirm_side_effects: bool = False
    force: bool = False


class UsageReport(BaseModel):
    """Usage the worker actually observed. Omit fields that were not reported."""

    source: str | None = None
    marker: str | None = None
    amount: float | None = None
    currency: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    provider: str | None = None


class WorkerResult(BaseModel):
    """Unified worker completion report (done ≠ success)."""

    ok: bool
    summary: str
    outputs: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    recoverable: bool = True
    error: str | None = None
    usage: UsageReport | None = None


class VerificationResult(BaseModel):
    passed: bool
    checks: list[dict[str, Any]] = Field(default_factory=list)
    message: str = ""
