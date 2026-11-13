from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from max_agent.api.app import create_app
from max_agent.api.token_registry import TokenRegistryError, authenticate, load_registry

try:  # scope constants live with the token registry
    from max_agent.api.scopes import (
        APPROVE,
        CANCEL,
        CREATE,
        DISPATCH,
        EXECUTE_INLINE,
        OPS,
        READ,
    )
except ImportError:  # pragma: no cover - keeps the module importable
    APPROVE = "approve"
    CANCEL = "cancel"
    CREATE = "create"
    DISPATCH = "dispatch"
    EXECUTE_INLINE = "execute-inline"
    OPS = "ops"
    READ = "read"


def _write(tmp_path, payload):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _record(secret: str, scopes: list[str], enabled: bool = True) -> dict:
    return {
        "secret_sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
        "scopes": scopes,
        "enabled": enabled,
    }


def test_authenticate_matches_sha256_of_secret(tmp_path, monkeypatch):
    path = _write(tmp_path, {"tok-a": _record("s3cret", ["read"])})
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    context = authenticate("s3cret")
    assert context is not None
    assert context.token_id == "tok-a"
    assert context.scopes == frozenset({"read"})
    assert context.principal == "tester"


def test_authenticate_returns_none_for_unknown_secret(tmp_path, monkeypatch):
    path = _write(tmp_path, {"tok-a": _record("s3cret", ["read"])})
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    assert authenticate("wrong") is None
    assert authenticate(None) is None


def test_disabled_token_is_rejected(tmp_path, monkeypatch):
    path = _write(tmp_path, {"tok-a": _record("s3cret", ["read"], enabled=False)})
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    assert authenticate("s3cret") is None


def test_registry_rejects_dispatch_plus_approve(tmp_path, monkeypatch):
    path = _write(tmp_path, {"tok-a": _record("s3cret", ["dispatch", "approve"])})
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    try:
        load_registry()
    except TokenRegistryError as exc:
        assert "approve" in str(exc)
    else:
        raise AssertionError("dispatch+approve 组合必须被拒绝")


def test_registry_rejects_create_plus_approve(tmp_path, monkeypatch):
    path = _write(tmp_path, {"tok-a": _record("s3cret", ["create", "approve"])})
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    try:
        load_registry()
    except TokenRegistryError as exc:
        assert "approve" in str(exc)
    else:
        raise AssertionError("create+approve 组合必须被拒绝")


def test_registry_rejects_unknown_scope(tmp_path, monkeypatch):
    path = _write(tmp_path, {"tok-a": _record("s3cret", ["read", "godmode"])})
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    try:
        load_registry()
    except TokenRegistryError:
        pass
    else:
        raise AssertionError("未知 scope 必须被拒绝")


def test_registry_rejects_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(tmp_path / "nope.json"))
    try:
        load_registry()
    except TokenRegistryError:
        pass
    else:
        raise AssertionError("注册表缺失必须报错而不是退化为单枚全权令牌")


def test_registry_is_hot_reloaded_so_revocation_is_immediate(tmp_path, monkeypatch):
    path = _write(tmp_path, {"tok-a": _record("s3cret", ["read"])})
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    assert authenticate("s3cret") is not None

    path.write_text(json.dumps({"tok-a": _record("s3cret", ["read"], enabled=False)}), encoding="utf-8")
    assert authenticate("s3cret") is None


def test_legacy_env_token_is_not_accepted(tmp_path, monkeypatch):
    path = _write(tmp_path, {"tok-a": _record("other", ["read"])})
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    monkeypatch.setenv("MAX_AGENT_API_TOKEN", "legacy-token")
    assert authenticate("legacy-token") is None


# ---------------------------------------------------------------------------
# HTTP scope matrix
#
# Every token below carries exactly one scope, so no token can create and
# approve (or dispatch and approve) the same work.
# ---------------------------------------------------------------------------

TOKEN_SCOPES = {
    "test-read": READ,
    "test-create": CREATE,
    "test-approve": APPROVE,
    "test-dispatch": DISPATCH,
    "test-cancel": CANCEL,
    "test-execute-inline": EXECUTE_INLINE,
    "test-ops": OPS,
}

CONSOLE_TOKENS = ("test-read", "test-create", "test-approve", "test-dispatch", "test-cancel")
OPERATOR_TOKENS = ("test-execute-inline", "test-ops")

MISSING_TASK = "does-not-exist"


def _scope_name(scope) -> str:
    """Scope constants may be plain strings or enum members."""
    return scope.value if hasattr(scope, "value") else str(scope)


def _bearer(headers) -> str | None:
    if not isinstance(headers, dict):
        return None
    raw = headers.get("Authorization") or headers.get("authorization") or ""
    if not raw:
        return None
    return raw.split(" ", 1)[-1].strip() or None


class _Tokens:
    """token id -> request headers carrying that token's bearer secret."""

    def __init__(self, headers_by_id: dict[str, dict[str, str]]) -> None:
        self._headers_by_id = headers_by_id

    def __getitem__(self, token_id: str) -> dict[str, str]:
        return dict(self._headers_by_id[token_id])


@pytest.fixture()
def tokens(request, tmp_path, monkeypatch) -> _Tokens:
    """Headers for one single-scope token per scope.

    Uses the shared `auth_header` fixture when it provides these tokens, and
    otherwise writes its own registry so the matrix stays self-contained.
    """
    auth_header = None
    try:
        auth_header = request.getfixturevalue("auth_header")
    except Exception:
        auth_header = None

    headers_by_id: dict[str, dict[str, str]] = {}
    if auth_header is not None:
        for token_id, scope in TOKEN_SCOPES.items():
            try:
                headers = auth_header(token_id)
            except Exception:
                continue
            secret = _bearer(headers) if isinstance(headers, dict) else None
            if not secret:
                continue
            # Only trust the shared token when it really carries this scope.
            try:
                context = authenticate(secret)
            except Exception:
                continue
            if context is None or _scope_name(scope) not in context.scopes:
                continue
            headers_by_id[token_id] = dict(headers)
    if len(headers_by_id) == len(TOKEN_SCOPES):
        return _Tokens(headers_by_id)

    secrets = {token_id: f"secret-{token_id}" for token_id in TOKEN_SCOPES}
    records = {
        token_id: _record(secret, [_scope_name(TOKEN_SCOPES[token_id])])
        for token_id, secret in secrets.items()
    }
    path = _write(tmp_path, records)
    monkeypatch.setenv("MAX_AGENT_API_TOKEN_REGISTRY", str(path))
    return _Tokens(
        {token_id: {"Authorization": f"Bearer {secret}"} for token_id, secret in secrets.items()}
    )


@pytest.fixture()
def client(db_url: str, tokens: _Tokens):
    with TestClient(create_app()) as test_client:
        yield test_client


def _task_body(request) -> dict:
    """A minimal valid task body; reuses the shared fixture when available."""
    fallback = {"goal": "scope matrix probe"}
    try:
        body = request.getfixturevalue("make_task_body")
    except Exception:
        return fallback
    if callable(body):
        try:
            body = body()
        except TypeError:
            return fallback
    return body if isinstance(body, dict) else fallback


def test_read_token_cannot_create(client, tokens, request):
    response = client.post("/v1/tasks", json=_task_body(request), headers=tokens["test-read"])
    assert response.status_code == 403


def test_read_token_cannot_cancel(client, tokens):
    response = client.post(
        f"/v1/tasks/{MISSING_TASK}/cancel", headers=tokens["test-read"]
    )
    assert response.status_code == 403


def test_create_token_cannot_approve(client, tokens):
    response = client.post(
        f"/v1/tasks/{MISSING_TASK}/approve", headers=tokens["test-create"]
    )
    assert response.status_code == 403


def test_create_token_cannot_dispatch(client, tokens):
    response = client.post(
        f"/v1/tasks/{MISSING_TASK}/dispatch", headers=tokens["test-create"]
    )
    assert response.status_code == 403


def test_approve_token_cannot_create(client, tokens, request):
    response = client.post("/v1/tasks", json=_task_body(request), headers=tokens["test-approve"])
    assert response.status_code == 403


def test_approve_token_cannot_dispatch(client, tokens):
    response = client.post(
        f"/v1/tasks/{MISSING_TASK}/dispatch", headers=tokens["test-approve"]
    )
    assert response.status_code == 403


def test_dispatch_token_cannot_approve(client, tokens):
    response = client.post(
        f"/v1/tasks/{MISSING_TASK}/approve", headers=tokens["test-dispatch"]
    )
    assert response.status_code == 403


def test_dispatch_token_cannot_create(client, tokens, request):
    response = client.post("/v1/tasks", json=_task_body(request), headers=tokens["test-dispatch"])
    assert response.status_code == 403


def test_console_tokens_cannot_schedule_or_run_queue(client, tokens):
    for token_id in CONSOLE_TOKENS:
        headers = tokens[token_id]
        scheduled = client.post(f"/v1/tasks/{MISSING_TASK}/schedule", headers=headers)
        assert scheduled.status_code == 403, token_id
        queued = client.post("/v1/queue/run", headers=headers)
        assert queued.status_code == 403, token_id


def test_console_tokens_cannot_retry_aer(client, tokens):
    for token_id in CONSOLE_TOKENS:
        response = client.post("/v1/aer/retry", headers=tokens[token_id])
        assert response.status_code == 403, token_id


def test_operator_scoped_tokens_are_accepted(client, tokens):
    scheduled = client.post(
        f"/v1/tasks/{MISSING_TASK}/schedule", headers=tokens["test-execute-inline"]
    )
    assert scheduled.status_code != 403
    retried = client.post("/v1/aer/retry", headers=tokens["test-ops"])
    assert retried.status_code != 403


def test_telemetry_requires_read_scope(client, tokens):
    allowed = client.get("/v1/telemetry", headers=tokens["test-read"])
    assert allowed.status_code == 200
    denied = client.get("/v1/telemetry", headers=tokens["test-create"])
    assert denied.status_code == 403


def test_protected_get_requires_read(client, tokens):
    allowed = client.get("/v1/tasks", headers=tokens["test-read"])
    assert allowed.status_code == 200
    denied = client.get("/v1/tasks", headers=tokens["test-create"])
    assert denied.status_code == 403


def test_webhook_still_requires_signature_not_bearer(client, tokens, monkeypatch):
    # Unset secret is 503 "ingress disabled". Enable ingress so the
    # assertion checks signature auth, and that a Bearer token cannot skip it.
    monkeypatch.setenv("MAX_AGENT_WEBHOOK_SECRET", "unit-test-secret")
    payload = {"idempotency_key": "scope-matrix-webhook", "goal": "webhook probe"}
    unsigned = client.post("/v1/inbound/webhook", json=payload)
    assert unsigned.status_code in (401, 403)

    bearer_only = client.post(
        "/v1/inbound/webhook", json=payload, headers=tokens["test-read"]
    )
    assert bearer_only.status_code in (401, 403)
