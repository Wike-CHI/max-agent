"""Unified worker adapter interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from max_agent.models.enums import WorkerKind
from max_agent.models.schemas import WorkerResult


@dataclass
class WorkerContext:
    task_id: str
    goal: str
    acceptance_criteria: list[str]
    payload: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 300
    # callback to journal tool calls without coupling to DB
    on_tool_call: Callable[[str, dict[str, Any]], None] | None = None


class Worker(ABC):
    kind: WorkerKind

    @abstractmethod
    def run(self, ctx: WorkerContext) -> WorkerResult:
        """Execute work. Reporting done does not imply task success."""

    @abstractmethod
    def capabilities(self) -> set[str]:
        """Capability tags for future matching."""
