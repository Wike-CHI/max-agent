from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from max_agent.api.app import create_app
from max_agent.api.console import mount_console


def test_source_only_install_has_helpful_console_response(tmp_path: Path) -> None:
    app = FastAPI()
    mount_console(app, tmp_path / "missing")
    client = TestClient(app)
    response = client.get("/console")
    assert response.status_code == 503
    assert "npm run build" in response.text


def test_static_console_and_assets_do_not_expose_parent_files(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>MAX</h1>")
    (dist / "app.js").write_text("console.info('MAX')")
    (tmp_path / "private.txt").write_text("not public")
    app = FastAPI()
    mount_console(app, dist)
    client = TestClient(app)
    assert client.get("/console/").text == "<h1>MAX</h1>"
    assert client.get("/console/app.js").status_code == 200
    assert client.get("/console/not-found.js").status_code == 404
    assert client.get("/console/%2e%2e/private.txt").status_code == 404


def test_console_security_headers_and_api_no_store(db_url: str, auth_header) -> None:
    client = TestClient(create_app())
    response = client.get("/console/")
    assert response.status_code in (200, 503)
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "test-api-token" not in response.text
    response = client.get("/v1/tasks", headers=auth_header("test-read"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
