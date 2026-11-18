"""SQLAlchemy 2 ORM models — SQLite now, PostgreSQL-compatible types."""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from max_agent.db.base import Base
from max_agent.db.types import ExactDecimal


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key"),
        Index("ix_tasks_status_priority", "status", "priority"),
        Index("ix_tasks_status_lease", "status", "lease_expires_at"),
        Index("ix_tasks_created_by", "created_by"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="user", server_default="user")
    acceptance_criteria: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    dependencies: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    max_cost_units: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    allow_auto_retry_side_effects: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    worker_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="mock")
    worker_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    has_side_effects: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tags: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    leased_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approval_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_scope: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    approval_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    issue_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    budget_ceilings: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # optimistic lock
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    events: Mapped[list[TaskEventRow]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskEventRow.id"
    )
    attempt_rows: Mapped[list[TaskAttemptRow]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskAttemptRow.attempt_no"
    )


class TaskEventRow(Base):
    __tablename__ = "task_events"
    __table_args__ = (Index("ix_task_events_task_created", "task_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    task: Mapped[TaskRow] = relationship(back_populates="events")


class TaskAttemptRow(Base):
    """One worker execution attempt plus the lease generation that owned it."""

    __tablename__ = "task_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_no", name="uq_task_attempts_task_no"),
        Index("ix_task_attempts_task_fence", "task_id", "fencing_token"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_owner: Mapped[str] = mapped_column(String(128), nullable=False)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    recoverable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    worker_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    runtime_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    cost_marker: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    cost_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    task: Mapped[TaskRow] = relationship(back_populates="attempt_rows")


class UsageRecordRow(Base):
    """One idempotent observed usage report; tokens do not imply a bill."""

    __tablename__ = "usage_records"
    __table_args__ = (
        UniqueConstraint("source_event_key", name="uq_usage_records_source_event_key"),
        Index("ix_usage_records_attempt", "attempt_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(36), ForeignKey("task_attempts.id"), nullable=False)
    source_event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(16), nullable=False, default="usage.v1")
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    billing_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compute_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CostEntryRow(Base):
    """A valued reading in its original unit, separate from token usage."""

    __tablename__ = "cost_entries"
    __table_args__ = (UniqueConstraint("usage_record_id", name="uq_cost_entries_usage_record"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    usage_record_id: Mapped[str] = mapped_column(String(36), ForeignKey("usage_records.id"), nullable=False)
    price_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("price_versions.id", name="fk_cost_entries_price_version"), nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    billing_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ProviderAccountRow(Base):
    """One owner's billing account; credential_ref never contains a token."""

    __tablename__ = "provider_accounts"
    __table_args__ = (
        UniqueConstraint("owner", "account_key", name="uq_provider_accounts_owner_key"),
        Index("ix_provider_accounts_owner_provider", "owner", "provider_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    account_key: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    billing_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ModelProfileRow(Base):
    """Provider model capabilities; performance scores are observed elsewhere."""

    __tablename__ = "model_profiles"
    __table_args__ = (UniqueConstraint("provider_key", "model_key", name="uq_model_profiles_provider_model"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    model_key: Mapped[str] = mapped_column(String(128), nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    supported_efforts: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class PriceVersionRow(Base):
    """Append-only price; latest effective_from wins until the next version."""

    __tablename__ = "price_versions"
    __table_args__ = (
        UniqueConstraint("account_id", "model_id", "effective_from", name="uq_price_versions_scope_start"),
        Index("ix_price_versions_scope_start", "account_id", "model_id", "effective_from"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("provider_accounts.id"), nullable=False)
    model_id: Mapped[str] = mapped_column(String(36), ForeignKey("model_profiles.id"), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pricing_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    input_rate: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    output_rate: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    cached_input_rate: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    compute_rate: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    source: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class BudgetPolicyRow(Base):
    """One owner/scope/unit/period ceiling; reservations lock these rows."""

    __tablename__ = "budget_policies"
    __table_args__ = (
        UniqueConstraint("owner", "scope", "scope_id", "unit", "period_start", name="uq_budget_policy_scope_start"),
        Index("ix_budget_policy_lookup", "owner", "scope", "scope_id", "unit"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ceiling: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
    warning_at: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class BudgetReservationRow(Base):
    """One attempt's bounded claim, retained through unknown-cost recovery."""

    __tablename__ = "budget_reservations"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_budget_reservations_attempt"),
        UniqueConstraint("idempotency_key", name="uq_budget_reservations_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("provider_accounts.id"), nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(36), ForeignKey("task_attempts.id"), nullable=False)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    reserved: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
    actual: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    evidence_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class BudgetHoldRow(Base):
    """Policy-specific commitment; no cross-currency total is computed."""

    __tablename__ = "budget_holds"
    __table_args__ = (UniqueConstraint("reservation_id", "policy_id", name="uq_budget_holds_reservation_policy"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reservation_id: Mapped[str] = mapped_column(String(36), ForeignKey("budget_reservations.id"), nullable=False)
    policy_id: Mapped[str] = mapped_column(String(36), ForeignKey("budget_policies.id"), nullable=False)
    committed: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)


class RouteDecisionRow(Base):
    """One policy snapshot bound to the attempt and its budget reservation."""

    __tablename__ = "route_decisions"
    __table_args__ = (UniqueConstraint("attempt_id", name="uq_route_decisions_attempt"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(36), ForeignKey("task_attempts.id"), nullable=False)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    reservation_id: Mapped[str] = mapped_column(String(36), ForeignKey("budget_reservations.id"), nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("provider_accounts.id"), nullable=False)
    model_id: Mapped[str] = mapped_column(String(36), ForeignKey("model_profiles.id"), nullable=False)
    price_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("price_versions.id"), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    vendor: Mapped[str] = mapped_column(String(16), nullable=False)
    model_key: Mapped[str] = mapped_column(String(128), nullable=False)
    effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    upper_bound: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AERExportRow(Base):
    """Outbox for one journal event. Delivery failure stays on this row."""

    __tablename__ = "aer_exports"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_aer_exports_event_id"),
        Index("ix_aer_exports_status", "status"),
        Index("ix_aer_exports_task", "task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey("task_events.id"), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped[TaskRow] = relationship()
    event: Mapped[TaskEventRow] = relationship()


class InboundDeliveryRow(Base):
    """One webhook/Routine delivery. Replays of delivery_id do not create a second task."""

    __tablename__ = "inbound_deliveries"
    __table_args__ = (Index("ix_inbound_idempotency", "idempotency_key"),)

    delivery_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
