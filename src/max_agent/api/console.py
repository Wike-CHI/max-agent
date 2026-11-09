"""Serve the optional, built read-only console from the API's origin."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

CONSOLE_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"


def mount_console(app: FastAPI, directory: Path | None = None) -> None:
    """A source-only Python install still starts without frontend build artifacts."""
    directory = directory if directory is not None else CONSOLE_DIST
    if (directory / "index.html").is_file():
        app.mount("/console", StaticFiles(directory=directory, html=True), name="console")
        return

    @app.get("/console", include_in_schema=False)
    def console_redirect() -> RedirectResponse:
        return RedirectResponse("/console/")

    @app.get("/console/", include_in_schema=False)
    def console_not_built() -> PlainTextResponse:
        return PlainTextResponse(
            "MAX console is not built. In the repository, run: "
            "cd frontend && npm ci && npm run build. Then restart the API.",
            status_code=503,
        )
