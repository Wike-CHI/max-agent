"""Task and event enumerations."""

from enum import StrEnum


class TaskStatus(StrEnum):
    """Lifecycle states for a first-class Task.

    Semantics:
    - pending: created, not yet claimed by scheduler
    - ready: dependencies satisfied, eligible for worker
    - leased: claimed by a worker (lease reserved for multi-worker)
    - running: worker actively executing
    - verifying: worker reported done; kernel verifying acceptance
    - succeeded: acceptance criteria passed
    - failed: terminal failure (max attempts / non-recoverable)
    - blocked: waiting on dependency or human approval
    - cancelled: user cancelled
    - retry_wait: recoverable failure; waiting to retry
    """

    PENDING = "pending"
    READY = "ready"
    LEASED = "leased"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    RETRY_WAIT = "retry_wait"


class EventType(StrEnum):
    CREATED = "created"
    STATUS_CHANGED = "status_changed"
    SCHEDULED = "scheduled"
    LEASED = "leased"
    ATTEMPT_STARTED = "attempt_started"
    ATTEMPT_FINISHED = "attempt_finished"
    TOOL_CALL = "tool_call"
    VERIFICATION = "verification"
    WORKER_OUTPUT = "worker_output"
    ERROR = "error"
    CANCELLED = "cancelled"
    APPROVAL_REQUIRED = "approval_required"
    IDEMPOTENT_HIT = "idempotent_hit"


class WorkerKind(StrEnum):
    MOCK = "mock"
    LOCAL_CODE = "local_code"
