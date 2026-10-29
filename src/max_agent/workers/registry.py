from __future__ import annotations

from max_agent.models.enums import WorkerKind
from max_agent.workers.base import Worker
from max_agent.workers.local_code import LocalCodeWorker
from max_agent.workers.mock import MockWorker

_REGISTRY: dict[WorkerKind, type[Worker]] = {
    WorkerKind.MOCK: MockWorker,
    WorkerKind.LOCAL_CODE: LocalCodeWorker,
}


def get_worker(kind: WorkerKind | str) -> Worker:
    k = WorkerKind(kind)
    cls = _REGISTRY.get(k)
    if cls is None:
        raise KeyError(f"no worker registered for {k}")
    return cls()


def list_workers() -> list[str]:
    return [k.value for k in _REGISTRY]
