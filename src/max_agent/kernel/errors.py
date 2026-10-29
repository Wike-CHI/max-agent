"""Lease and fencing errors. Subclass ValueError so HTTP maps them to 409."""


class LeaseHeldError(ValueError):
    """Another worker holds a lease that has not expired."""


class StaleFenceError(ValueError):
    """Finalize used a fencing token that is no longer the committed lease."""


class ConcurrencyLimitError(ValueError):
    """The queue is already at its in-flight lease cap."""


class CyclicDependencyError(ValueError):
    """The task graph contains a cycle. Nothing was stored."""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = list(cycle)
        path = " -> ".join(self.cycle)
        super().__init__(f"cyclic dependencies: {path}")


class IrreversibleOpError(RuntimeError):
    """An irreversible tool was refused. The external effect must not run."""
