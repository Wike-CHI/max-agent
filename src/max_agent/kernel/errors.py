"""Lease and fencing errors. Subclass ValueError so HTTP maps them to 409."""


class LeaseHeldError(ValueError):
    """Another worker holds a lease that has not expired."""


class StaleFenceError(ValueError):
    """Finalize used a fencing token that is no longer the committed lease."""
