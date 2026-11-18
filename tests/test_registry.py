"""Provider accounts and effective-dated prices use fixtures, not live rates."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select

from max_agent.api.app import create_app
from max_agent.db.models import TaskRow
from max_agent.db.session import reset_engine, session_scope
from max_agent.kernel.registry import RegistryService
from max_agent.kernel.service import TaskService
from max_agent.models.schemas import TaskCreate


def _day(month: int, day: int) -> datetime:
    return datetime(2026, month, day, tzinfo=timezone.utc)


def test_account_model_and_price_history_are_distinct_and_exact(session) -> None:
    registry = RegistryService(session)
    api = registry.register_account(
        owner="alice", account_key="openai-api", provider_key="openai",
        billing_mode="api_metered", credential_ref="env:OPENAI_API_KEY", enabled=True,
    )
    subscription = registry.register_account(
        owner="alice", account_key="codex-sub", provider_key="openai",
        billing_mode="subscription", enabled=False,
    )
    profile = registry.register_model(
        provider_key="openai", model_key="fixture-coder", capabilities=["tool_use", "coding", "coding"],
        supported_efforts=["high", "low"], context_window=128000, enabled=True,
    )
    first = registry.add_price(
        account_id=api.id, model_id=profile.id, effective_from=_day(11, 1),
        pricing_unit="million_tokens", currency="USD", source="fixture:v1",
        input_rate=Decimal("0.123456789012"), output_rate=Decimal("1.25"),
    )
    second = registry.add_price(
        account_id=api.id, model_id=profile.id, effective_from=_day(12, 1),
        pricing_unit="million_tokens", currency="USD", source="fixture:v2",
        input_rate=Decimal("0.10"), output_rate=Decimal("1.00"),
    )
    session.expire_all()
    assert first.input_rate == Decimal("0.123456789012")
    assert registry.price_at(owner="alice", account_id=api.id, model_id=profile.id, at=_day(11, 15)).id == first.id
    assert registry.price_at(owner="alice", account_id=api.id, model_id=profile.id, at=_day(12, 10)).id == second.id
    assert registry.price_at(owner="alice", account_id=api.id, model_id=profile.id, at=_day(10, 31)) is None
    assert registry.price_at(owner="alice", account_id=subscription.id, model_id=profile.id, at=_day(12, 10)) is None
    assert profile.capabilities == ["coding", "tool_use"]
    assert profile.supported_efforts == ["high", "low"]
    assert [a.account_key for a in registry.accounts("alice")] == ["codex-sub", "openai-api"]

    with pytest.raises(ValueError, match="effective-time order"):
        registry.add_price(account_id=api.id, model_id=profile.id, effective_from=_day(11, 15),
                           pricing_unit="million_tokens", currency="USD", source="fixture:backdate", input_rate=1)
    with pytest.raises(ValueError, match="unit or currency"):
        registry.add_price(account_id=api.id, model_id=profile.id, effective_from=_day(12, 15),
                           pricing_unit="million_tokens", currency="CNY", source="fixture:mismatch", input_rate=1)
    assert session.query(type(first)).count() == 2


def test_registry_rejects_keys_as_refs_and_mismatched_provider(session) -> None:
    registry = RegistryService(session)
    with pytest.raises(ValueError, match="credential_ref"):
        registry.register_account(owner="alice", account_key="bad", provider_key="openai",
                                  billing_mode="api_metered", credential_ref="sk-proj-secret-value")
    account = registry.register_account(owner="alice", account_key="api", provider_key="openai",
                                        billing_mode="api_metered")
    model = registry.register_model(provider_key="other", model_key="fixture", capabilities=["coding"])
    with pytest.raises(ValueError, match="same provider"):
        registry.add_price(account_id=account.id, model_id=model.id, effective_from=_day(11, 1),
                           pricing_unit="million_tokens", currency="USD", source="fixture:bad", input_rate=1)
    with pytest.raises(ValueError, match="price rate"):
        registry.add_price(account_id=account.id, model_id=registry.register_model(
            provider_key="openai", model_key="ok", capabilities=[]).id,
            effective_from=_day(11, 1), pricing_unit="million_tokens", currency="USD",
            source="fixture:bad", input_rate="NaN")


def test_read_only_registry_api_hides_refs_and_scopes_accounts(db_url, monkeypatch) -> None:
    monkeypatch.setenv("MAX_AGENT_API_ACTOR", "alice")
    with session_scope(db_url) as session:
        registry = RegistryService(session)
        own = registry.register_account(owner="alice", account_key="primary", provider_key="openai",
                                        billing_mode="api_metered", credential_ref="env:OPENAI_API_KEY", enabled=True)
        other = registry.register_account(owner="bob", account_key="private", provider_key="openai",
                                          billing_mode="api_metered", credential_ref="env:BOB_KEY")
        model = registry.register_model(provider_key="openai", model_key="fixture-coder", capabilities=["coding"])
        registry.add_price(account_id=own.id, model_id=model.id, effective_from=_day(11, 1),
                           pricing_unit="million_tokens", currency="USD", source="fixture:api",
                           input_rate="0.125", output_rate="1.5")
    client = TestClient(create_app(), headers={"Authorization": "Bearer test-api-token"})
    assert client.get("/v1/providers", headers={"Authorization": ""}).status_code == 401
    providers = client.get("/v1/providers")
    assert providers.status_code == 200
    assert [item["account_key"] for item in providers.json()] == ["primary"]
    assert "credential_ref" not in str(providers.json())
    models = client.get("/v1/models", params={"provider": "openai"})
    assert models.status_code == 200
    assert models.json()[0]["capabilities"] == ["coding"]
    current = client.get(f"/v1/models/{model.id}/prices/current", params={"account_id": own.id})
    assert current.status_code == 200
    assert current.json()["input_rate"] == "0.125"
    assert client.get(f"/v1/models/{model.id}/prices/current", params={"account_id": other.id}).status_code == 404
    assert client.post("/v1/providers", json={}).status_code == 405


def test_registry_migration_is_additive(tmp_path, monkeypatch) -> None:
    url = f"sqlite:///{tmp_path / 'registry.db'}"
    monkeypatch.setenv("MAX_AGENT_DATABASE_URL", url)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "a40b9c612e74")
    reset_engine()
    with session_scope(url) as session:
        task_id = TaskService(session).create(TaskCreate(goal="existing task")).id
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    assert {"provider_accounts", "model_profiles", "price_versions"} <= set(inspect(engine).get_table_names())
    assert "price_version_id" in {col["name"] for col in inspect(engine).get_columns("cost_entries")}
    with engine.connect() as conn:
        assert conn.execute(select(TaskRow.goal).where(TaskRow.id == task_id)).scalar_one() == "existing task"
    command.downgrade(cfg, "a40b9c612e74")
    assert "price_versions" not in inspect(engine).get_table_names()
    assert "usage_records" in inspect(engine).get_table_names()
    assert "price_version_id" not in {col["name"] for col in inspect(engine).get_columns("cost_entries")}
    with engine.connect() as conn:
        assert conn.execute(select(TaskRow.goal).where(TaskRow.id == task_id)).scalar_one() == "existing task"
    engine.dispose()
    reset_engine()
