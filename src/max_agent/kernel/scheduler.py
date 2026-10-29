"""Scheduler — claim, execute outside the session, then finalize.

The database lease (owner, expiry, fencing token) is the lock. An in-memory
LeaseManager cannot survive process death, so it is not consulted here.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from max_agent.db.models import TaskRow, utcnow
from max_agent.db.session import get_session_factory
from max_agent.kernel.errors import ConcurrencyLimitError, LeaseHeldError
from max_agent.kernel.execution import execute_claim, finalize_in_session
from max_agent.kernel.lease import AttemptClaim, claim_in_session, recover_expired_in_session
from max_agent.kernel.queue import QueuePolicy, pick_next
from max_agent.kernel.readmodel import task_to_read
from max_agent.models.enums import TaskStatus, WorkerKind
from max_agent.models.schemas import ScheduleRequest, TaskRead
from max_agent.workers.base import Worker


class LeaseManager:
    """Reserved. Durability lives in task_attempts + tasks.fencing_token."""

    def acquire(self, task_id: str, worker_id: str, ttl_seconds: int) -> bool: ...

    def heartbeat(self, task_id: str, worker_id: str) -> bool: ...

    def release(self, task_id: str, worker_id: str) -> None: ...


class CapabilityMatcher:
    """Reserved for matching task requirements to worker capabilities."""

    def match(self, required: set[str], available: set[str]) -> bool: ...


class NullLeaseManager(LeaseManager):
    def acquire(self, task_id: str, worker_id: str, ttl_seconds: int) -> bool:
        return True

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        return True

    def release(self, task_id: str, worker_id: str) -> None:
        return None


class SimpleCapabilityMatcher(CapabilityMatcher):
    def match(self, required: set[str], available: set[str]) -> bool:
        return required.issubset(available) if required else True


def _factory_from_session(session: Session) -> sessionmaker[Session]:
    bind = session.get_bind()
    engine = bind if isinstance(bind, Engine) else bind.engine
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Scheduler:
    """Pick a task, commit its lease, run the worker, commit the outcome."""

    def __init__(
        self,
        session: Session | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
        worker_id: str = "worker-1",
        leases: LeaseManager | None = None,
        matcher: CapabilityMatcher | None = None,
        lease_ttl_seconds: int = 600,
        backoff_base_seconds: float = 1.0,
        backoff_cap_seconds: float = 300.0,
        worker_resolver: Callable[[WorkerKind], Worker] | None = None,
        max_concurrency: int = 1,
        starve_after_seconds: float = 3600.0,
        fairness_step: int = 10,
    ) -> None:
        self._external = session
        if session_factory is not None:
            self.session_factory = session_factory
        elif session is not None:
            self.session_factory = _factory_from_session(session)
        else:
            self.session_factory = get_session_factory()
        # Kept so older callers can still pass them. The DB fence is authoritative.
        self.leases = leases or NullLeaseManager()
        self.matcher = matcher or SimpleCapabilityMatcher()
        self.worker_id = worker_id
        self.lease_ttl_seconds = lease_ttl_seconds
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_cap_seconds = backoff_cap_seconds
        self.worker_resolver = worker_resolver
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self.policy = QueuePolicy(
            max_concurrency=max_concurrency,
            starve_after_seconds=starve_after_seconds,
            fairness_step=fairness_step,
        )

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _sync_external(self) -> None:
        """Make caller-session writes visible, then drop stale identity-map state."""
        if self._external is None:
            return
        self._external.commit()
        self._external.expire_all()

    def recover_expired(self) -> list[TaskRead]:
        """Requeue or block tasks whose lease expired. Safe to call on process start."""
        self._sync_external()
        now = utcnow()
        with self._transaction() as session:
            changed = recover_expired_in_session(
                session,
                now=now,
                backoff_base_seconds=self.backoff_base_seconds,
                backoff_cap_seconds=self.backoff_cap_seconds,
            )
            rows = [session.get(TaskRow, task_id) for task_id in changed]
            reads = [task_to_read(row) for row in rows if row is not None]
        self._sync_external()
        return reads

    def acquire_lease(self, task_id: str, req: ScheduleRequest | None = None) -> AttemptClaim:
        """Commit a lease and return. Caller executes, then finalize_lease."""
        self._sync_external()
        req = req or ScheduleRequest()
        with self._transaction() as session:
            outcome = claim_in_session(
                session,
                task_id=task_id,
                worker_id=self.worker_id,
                now=utcnow(),
                ttl_seconds=self.lease_ttl_seconds,
                req=req,
                automatic=False,
                backoff_base_seconds=self.backoff_base_seconds,
                backoff_cap_seconds=self.backoff_cap_seconds,
                max_concurrency=self.policy.max_concurrency,
            )
        self._sync_external()
        if not isinstance(outcome, AttemptClaim):
            raise ValueError(f"task {task_id} was not claimed (status={outcome.status})")
        return outcome

    def finalize_lease(self, claim: AttemptClaim, result: WorkerResult) -> TaskRead:
        """Commit the outcome if `claim.fencing_token` is still current."""
        with self._transaction() as session:
            out = finalize_in_session(
                session,
                claim,
                result,
                None,
                now=utcnow(),
                backoff_base_seconds=self.backoff_base_seconds,
                backoff_cap_seconds=self.backoff_cap_seconds,
            )
        self._sync_external()
        return out

    def run_one(self, task_id: str | None = None, req: ScheduleRequest | None = None) -> TaskRead:
        self._sync_external()
        req = req or ScheduleRequest()
        now = utcnow()
        with self._transaction() as session:
            recovered = recover_expired_in_session(
                session,
                now=now,
                backoff_base_seconds=self.backoff_base_seconds,
                backoff_cap_seconds=self.backoff_cap_seconds,
            )
        try:
            if task_id is None:
                return self._run_automatic(req)
            if recovered.get(task_id) == TaskStatus.FAILED.value:
                return self._read(task_id)
            return self._run_id(task_id, req, automatic=False)
        finally:
            self._sync_external()

    def run_queue(self, *, max_tasks: int | None = None, req: ScheduleRequest | None = None) -> list[TaskRead]:
        """Run ready tasks until the queue is empty, the cap is hit, or `max_tasks` finish."""
        req = req or ScheduleRequest()
        done: list[TaskRead] = []
        while max_tasks is None or len(done) < max_tasks:
            try:
                task = self.run_one(None, req)
            except LookupError:
                break
            except ConcurrencyLimitError:
                break
            done.append(task)
            if task.status in {TaskStatus.READY, TaskStatus.PENDING}:
                break
        return done

    def _run_automatic(self, req: ScheduleRequest) -> TaskRead:
        excluded: set[str] = set()
        while True:
            with self._transaction() as session:
                pick = pick_next(session, utcnow(), excluded, self.policy)
            if pick.reason == "concurrency":
                raise ConcurrencyLimitError(f"concurrency limit {self.policy.max_concurrency} reached")
            if pick.task_id is None:
                raise LookupError("no ready tasks")
            try:
                return self._run_id(pick.task_id, req, automatic=True)
            except LeaseHeldError:
                excluded.add(pick.task_id)

    def _run_id(self, task_id: str, req: ScheduleRequest, *, automatic: bool) -> TaskRead:
        with self._transaction() as session:
            outcome = claim_in_session(
                session,
                task_id=task_id,
                worker_id=self.worker_id,
                now=utcnow(),
                ttl_seconds=self.lease_ttl_seconds,
                req=req,
                automatic=automatic,
                backoff_base_seconds=self.backoff_base_seconds,
                backoff_cap_seconds=self.backoff_cap_seconds,
                max_concurrency=self.policy.max_concurrency,
            )
        if not isinstance(outcome, AttemptClaim):
            return outcome
        result, hint = execute_claim(
            outcome,
            session_factory=self.session_factory,
            worker_resolver=self.worker_resolver,
        )
        with self._transaction() as session:
            return finalize_in_session(
                session,
                outcome,
                result,
                hint,
                now=utcnow(),
                backoff_base_seconds=self.backoff_base_seconds,
                backoff_cap_seconds=self.backoff_cap_seconds,
            )

    def _read(self, task_id: str) -> TaskRead:
        with self._transaction() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise KeyError(f"task not found: {task_id}")
            return task_to_read(row)
