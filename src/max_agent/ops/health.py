"""Process liveness and database readiness. Responses never echo secrets."""

from __future__ import annotations

from max_agent import __version__


class DatabaseUnavailable(RuntimeError):
    pass


def liveness() -> dict[str, str]:
    return {"status": "ok", "service": "max-agent", "version": __version__}


def database_ready() -> None:
    from sqlalchemy import text

    from max_agent.db.session import get_engine

    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except DatabaseUnavailable:
        raise
    except Exception as exc:
        raise DatabaseUnavailable("database unavailable") from exc
