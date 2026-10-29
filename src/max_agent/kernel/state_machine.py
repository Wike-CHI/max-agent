"""Legal task status transitions."""

from __future__ import annotations

from max_agent.models.enums import TaskStatus

# from -> frozenset of allowed next states
TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.WAITING_APPROVAL, TaskStatus.CANCELLED}
    ),
    TaskStatus.READY: frozenset(
        {TaskStatus.LEASED, TaskStatus.BLOCKED, TaskStatus.WAITING_APPROVAL, TaskStatus.CANCELLED}
    ),
    TaskStatus.LEASED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.READY,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.RETRY_WAIT,
            TaskStatus.BLOCKED,
            TaskStatus.WAITING_APPROVAL,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.VERIFYING,
            TaskStatus.RETRY_WAIT,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.VERIFYING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.RETRY_WAIT,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RETRY_WAIT: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED,
            TaskStatus.WAITING_APPROVAL,
        }
    ),
    TaskStatus.BLOCKED: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.PENDING,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.WAITING_APPROVAL: frozenset(
        {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.SUCCEEDED: frozenset(),  # terminal
    TaskStatus.FAILED: frozenset({TaskStatus.READY}),  # manual re-open only
    TaskStatus.CANCELLED: frozenset(),  # terminal
}

TERMINAL = frozenset({TaskStatus.SUCCEEDED, TaskStatus.CANCELLED})


class IllegalTransitionError(ValueError):
    def __init__(self, current: TaskStatus, target: TaskStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Illegal transition: {current} -> {target}")


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    if current == target:
        return True
    return target in TRANSITIONS.get(current, frozenset())


def assert_transition(current: TaskStatus, target: TaskStatus) -> None:
    if not can_transition(current, target):
        raise IllegalTransitionError(current, target)
