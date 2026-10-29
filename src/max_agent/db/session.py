"""Database engine / session helpers."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from max_agent.db.base import Base

_DEFAULT_DB = Path(__file__).resolve().parents[3] / "data" / "max_agent.db"

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _resolve_url(url: str | None = None) -> str:
    if url:
        return url
    env = os.environ.get("MAX_AGENT_DATABASE_URL")
    if env:
        return env
    path = Path(os.environ.get("MAX_AGENT_DB_PATH", str(_DEFAULT_DB)))
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path}"


def get_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    global _engine, _SessionLocal
    resolved = _resolve_url(url)
    if _engine is None or (url and str(_engine.url) != resolved):
        connect_args = {"check_same_thread": False} if resolved.startswith("sqlite") else {}
        _engine = create_engine(resolved, echo=echo, future=True, connect_args=connect_args)
        if resolved.startswith("sqlite"):

            @event.listens_for(_engine, "connect")
            def _sqlite_pragma(dbapi_conn, _connection_record):  # type: ignore[no-untyped-def]
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()

        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return _engine


def get_session_factory(url: str | None = None) -> sessionmaker[Session]:
    get_engine(url)
    assert _SessionLocal is not None
    return _SessionLocal


def init_db(url: str | None = None) -> None:
    engine = get_engine(url)
    Base.metadata.create_all(bind=engine)


def reset_engine() -> None:
    """Test helper: drop cached engine."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    factory = get_session_factory(url)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
