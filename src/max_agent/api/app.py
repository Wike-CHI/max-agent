"""FastAPI HTTP adapter — thin over TaskService."""

from __future__ import annotations

from fastapi import FastAPI

from max_agent import __version__
from max_agent.api.routes import router
from max_agent.db.session import init_db


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(
        title="MAX-Agent",
        description="Task-centric personal agent scheduler kernel",
        version=__version__,
    )
    app.include_router(router)
    return app


app = create_app()
