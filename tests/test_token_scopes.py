from __future__ import annotations

import hashlib
import json

from max_agent.api.token_registry import TokenRegistryError, authenticate, load_registry


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
