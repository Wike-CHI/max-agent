"""Scheduler — single-worker first; interfaces reserved for multi-worker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from max_agent.db.models import TaskRow
from max_agent.kernel.service import TaskService
from max_agent.models.enums import TaskStatus
from max_agent.models.schemas import ScheduleRequest, TaskRead


class LeaseManager(Protocol):
    """Reserved for multi-worker lease acquisition / heartbeat."""

    def acquire(self, task_id: str, worker_id: str, ttl_seconds: int) -> bool: ...
    def heartbeat(self, task_id: str, worker_id: str) -> bool: ...
    def release(self, task_id: str, worker_id: str) -> None: ...


class CapabilityMatcher(Protocol):
    """Reserved for matching task requirements to worker capabilities."""

    def match(self, required: set[str], available: set[str]) -> bool: ...


@dataclass
class NullLeaseManager:
    def acquire(self, task_id: str, worker_id: str, ttl_seconds: int) -> bool:
        return True

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        return True

    def release(self, task_id: str, worker_id: str) -> None:
        return None


@dataclass
class SimpleCapabilityMatcher:
    def match(self, required: set[str], available: set[str]) -> bool:
        return required.issubset(available) if required else True


class Scheduler:
    """Pick next ready task and run via TaskService (single worker)."""

    def __init__(
        self,
        session: Session,
        *,
        worker_id: str = "worker-1",
        leases: LeaseManager | None = None,
        matcher: CapabilityMatcher | None = None,
    ) -> None:
        self.session = session
        self.service = TaskService(session, worker_id=worker_id)
        self.leases = leases or NullLeaseManager()
        self.matcher = matcher or SimpleCapabilityMatcher()
        self.worker_id = worker_id

    def next_ready(self) -> TaskRow | None:
        return (
            self.session.query(TaskRow)
            .filter(TaskRow.status.in_([TaskStatus.READY.value, TaskStatus.RETRY_WAIT.value]))
            .order_by(TaskRow.priority.asc(), TaskRow.created_at.asc())
            .first()
        )

    def run_one(self, task_id: str | None = None, req: ScheduleRequest | None = None) -> TaskRead:
        if task_id is None:
            row = self.next_ready()
            if row is None:
                raise LookupError("no ready tasks")
            task_id = row.id
        if not self.leases.acquire(task_id, self.worker_id, ttl_seconds=600):
            raise RuntimeError("failed to acquire lease")
        try:
            return self.service.schedule(task_id, req)
        finally:
            self.leases.release(task_id, self.worker_id)
