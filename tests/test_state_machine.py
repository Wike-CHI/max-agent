import pytest

from max_agent.kernel.state_machine import IllegalTransitionError, assert_transition, can_transition
from max_agent.models.enums import TaskStatus


def test_legal_happy_path():
    assert can_transition(TaskStatus.PENDING, TaskStatus.READY)
    assert can_transition(TaskStatus.READY, TaskStatus.LEASED)
    assert can_transition(TaskStatus.LEASED, TaskStatus.RUNNING)
    assert can_transition(TaskStatus.RUNNING, TaskStatus.VERIFYING)
    assert can_transition(TaskStatus.VERIFYING, TaskStatus.SUCCEEDED)


def test_illegal_jump():
    with pytest.raises(IllegalTransitionError):
        assert_transition(TaskStatus.PENDING, TaskStatus.SUCCEEDED)


def test_terminal_no_exit():
    assert not can_transition(TaskStatus.SUCCEEDED, TaskStatus.READY)
    assert not can_transition(TaskStatus.CANCELLED, TaskStatus.READY)
    # failed may reopen
    assert can_transition(TaskStatus.FAILED, TaskStatus.READY)
