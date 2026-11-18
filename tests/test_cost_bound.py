"""Conservative one-request quote, without a provider connection or paid call."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from max_agent.kernel.cost_bound import SingleRequestPlan, prepare_single_request_bound
from max_agent.kernel.registry import RegistryService


class FakeCounter:
    def __init__(self, count: int) -> None:
        self.count = count
        self.calls: list[dict[str, str]] = []

    def count_input_tokens(self, *, model: str, input: str) -> int:
        self.calls.append({"model": model, "input": input})
        return self.count


def _registry(session):
    service = RegistryService(session)
    account = service.register_account(owner="user", account_key="api", provider_key="openai",
                                       billing_mode="api_metered", credential_ref="env:OPENAI_API_KEY", enabled=True)
    model = service.register_model(provider_key="openai", model_key="gpt-test", capabilities=["text"],
                                   context_window=100_000, enabled=True)
    price = service.add_price(account_id=account.id, model_id=model.id,
                              effective_from=datetime.now(timezone.utc), pricing_unit="per_million_tokens",
                              currency="USD", source="https://example.org/price",
                              input_rate="2.50", output_rate="10", cached_input_rate="1.25")
    return account, model, price


def test_exact_full_rate_quote_and_immutable_payload(session) -> None:
    account, model, price = _registry(session)
    plan = SingleRequestPlan(model="gpt-test", input="Explain the code", max_input_tokens=1200,
                             max_output_tokens=2000)
    counter = FakeCounter(1000)
    bound = prepare_single_request_bound(plan, account, model, price, counter,
                                         owner="user", task_ceiling="0.03")
    assert bound.upper_bound == Decimal("0.0225")
    assert bound.unit == "USD" and bound.input_tokens == 1000
    assert bound.price_version_id == price.id
    assert counter.calls == [plan.count_payload()]
    assert plan.create_payload()["max_output_tokens"] == 2000
    assert plan.create_payload()["tools"] == []
    assert len(bound.request_sha256) == 64
    assert bound.request == plan
    with pytest.raises(ValueError, match="task ceiling"):
        prepare_single_request_bound(plan, account, model, price, FakeCounter(1000),
                                     owner="user", task_ceiling="0.0224")


def test_rounds_tiny_worst_case_up_and_uses_higher_cache_rate(session) -> None:
    account, model, price = _registry(session)
    price.input_rate = Decimal("0.000000000000000001")
    price.output_rate = Decimal("0")
    price.cached_input_rate = Decimal("0")
    plan = SingleRequestPlan(model="gpt-test", input="x", max_input_tokens=1, max_output_tokens=16)
    bound = prepare_single_request_bound(plan, account, model, price, FakeCounter(1),
                                         owner="user", task_ceiling="0.000000000000000001")
    assert bound.upper_bound == Decimal("0.000000000000000001")
    price.cached_input_rate = Decimal("3")
    assert prepare_single_request_bound(plan, account, model, price, FakeCounter(1),
                                        owner="user", task_ceiling="1").upper_bound == Decimal("0.000003")


@pytest.mark.parametrize("count", [0, 1201, True])
def test_untrusted_count_fails_closed(session, count) -> None:
    account, model, price = _registry(session)
    plan = SingleRequestPlan(model="gpt-test", input="x", max_input_tokens=1200, max_output_tokens=16)
    with pytest.raises(ValueError, match="input token count"):
        prepare_single_request_bound(plan, account, model, price, FakeCounter(count),
                                     owner="user", task_ceiling="1")


def test_unsupported_price_account_and_context_fail_closed(session) -> None:
    account, model, price = _registry(session)
    plan = SingleRequestPlan(model="gpt-test", input="x", max_input_tokens=1200, max_output_tokens=16)
    with pytest.raises(ValueError, match="bound"):
        prepare_single_request_bound(plan, account, model, price, FakeCounter(1),
                                     owner="other", task_ceiling="1")
    price.pricing_unit = "tokens"
    with pytest.raises(ValueError, match="billing dimensions"):
        prepare_single_request_bound(plan, account, model, price, FakeCounter(1),
                                     owner="user", task_ceiling="1")
    price.pricing_unit = "per_million_tokens"
    model.context_window = 16
    with pytest.raises(ValueError, match="context window"):
        prepare_single_request_bound(plan, account, model, price, FakeCounter(1),
                                     owner="user", task_ceiling="1")


def test_invalid_plan_does_not_call_counter(session) -> None:
    account, model, price = _registry(session)
    with pytest.raises(ValueError, match="invalid single-request"):
        SingleRequestPlan(model="gpt-test", input="x", max_input_tokens=10, max_output_tokens=0)
    counter = FakeCounter(1)
    price.compute_rate = Decimal("0.1")
    with pytest.raises(ValueError, match="billing dimensions"):
        prepare_single_request_bound(SingleRequestPlan("gpt-test", "x", 10, 16),
                                     account, model, price, counter, owner="user", task_ceiling="1")
    assert counter.calls == []
