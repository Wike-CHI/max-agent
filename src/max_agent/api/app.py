"""FastAPI HTTP adapter — thin over TaskService."""

from __future__ import annotations

from fastapi import FastAPI, Request

from max_agent import __version__
from max_agent.api.routes import router
from max_agent.api.console import mount_console
from max_agent.db.session import init_db
from max_agent.ops.secrets import install_secret_filter
from max_agent.ops.telemetry import get_telemetry


def create_app() -> FastAPI:
    install_secret_filter()
    init_db()
    app = FastAPI(
        title="MAX-Agent",
        description="Task-centric personal agent scheduler kernel",
        version=__version__,
    )
    app.include_router(router)
    mount_console(app)

    @app.middleware("http")
    async def _http_span(request: Request, call_next):  # type: ignore[no-untyped-def]
        # Path only. Query strings and headers can carry secrets.
        with get_telemetry().span("http.request", method=request.method, path=request.url.path) as span:
            try:
                response = await call_next(request)
            except Exception:
                span.set("status_code", 500)
                get_telemetry().counter("http.server.errors")
                raise
            span.set("status_code", response.status_code)
            get_telemetry().counter("http.server.requests")
            if request.url.path.startswith(("/v1/", "/console")):
                response.headers["Cache-Control"] = "no-store"
                response.headers["X-Content-Type-Options"] = "nosniff"
                response.headers["Referrer-Policy"] = "no-referrer"
            if request.url.path.startswith("/console"):
                response.headers["Content-Security-Policy"] = (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
                    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
                )
            return response

    return app


app = create_app()
