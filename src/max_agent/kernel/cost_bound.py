"""Offline quote for one fixed, text-only Responses request.

This module prepares a bound; it does not authorize or send a paid request.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, localcontext
from typing import Protocol

from max_agent.db.models import ModelProfileRow, PriceVersionRow, ProviderAccountRow
from max_agent.kernel.budget import _amount

_ONE_MILLION = Decimal(1_000_000)
_STORAGE_QUANTUM = Decimal("0.000000000000000001")


class InputTokenCounter(Protocol):
    def count_input_tokens(self, *, model: str, input: str) -> int: ...


@dataclass(frozen=True)
class SingleRequestPlan:
    """No tools, conversation, files, images, streaming or follow-up request."""

    model: str
    input: str
    max_input_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", self.model)
                or not self.input or len(self.input) > 1_000_000
                or self.max_input_tokens < 1 or self.max_output_tokens < 16):
            raise ValueError("invalid single-request token limits")
        if self.max_input_tokens > 10_000_000 or self.max_output_tokens > 10_000_000:
            raise ValueError("token limit exceeds supported bound")

    def count_payload(self) -> dict[str, str]:
        return {"model": self.model, "input": self.input}

    def create_payload(self) -> dict[str, object]:
        return {
            **self.count_payload(), "max_output_tokens": self.max_output_tokens,
            "tools": [], "tool_choice": "none", "truncation": "disabled", "store": False,
        }

    def digest(self) -> str:
        encoded = json.dumps(self.create_payload(), sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PreparedCostBound:
    price_version_id: str
    request_sha256: str
    model: str
    unit: str
    input_tokens: int
    max_output_tokens: int
    upper_bound: Decimal
    request: SingleRequestPlan


def prepare_single_request_bound(
    plan: SingleRequestPlan, account: ProviderAccountRow, model: ModelProfileRow,
    price: PriceVersionRow, counter: InputTokenCounter,
    *, owner: str, task_ceiling: Decimal | str | int,
) -> PreparedCostBound:
    """Count the exact frozen text input, then quote full-rate worst-case tokens."""
    if (
        account.owner != owner or not account.enabled or account.provider_key != "openai"
        or account.billing_mode != "api_metered" or account.credential_ref != "env:OPENAI_API_KEY"
        or not model.enabled or model.provider_key != "openai" or model.model_key != plan.model
        or price.account_id != account.id or price.model_id != model.id
        or model.context_window is None
    ):
        raise ValueError("account, model and price are not bound to this request")
    if (
        price.pricing_unit != "per_million_tokens"
        or not price.source.startswith("https://")
        or price.input_rate is None or price.output_rate is None
        or price.compute_rate is not None
    ):
        raise ValueError("price does not cover the single-request billing dimensions")
    if price.cached_input_rate is not None and price.cached_input_rate < 0:
        raise ValueError("invalid cached input rate")
    input_rate = max(price.input_rate, price.cached_input_rate or Decimal(0))
    output_rate = price.output_rate
    if input_rate < 0 or output_rate < 0 or price.currency != "USD":
        raise ValueError("invalid or unsupported price")
    count = counter.count_input_tokens(**plan.count_payload())
    if isinstance(count, bool) or not isinstance(count, int) or count < 1 or count > plan.max_input_tokens:
        raise ValueError("input token count unavailable or above ceiling")
    if count + plan.max_output_tokens > model.context_window:
        raise ValueError("request exceeds model context window")
    with localcontext() as ctx:
        ctx.prec = 64
        raw = (Decimal(count) * input_rate + Decimal(plan.max_output_tokens) * output_rate) / _ONE_MILLION
        upper = raw.quantize(_STORAGE_QUANTUM, rounding=ROUND_CEILING)
    _amount(upper)
    if upper > _amount(task_ceiling):
        raise ValueError("request bound exceeds task ceiling")
    return PreparedCostBound(
        price_version_id=price.id, request_sha256=plan.digest(), model=plan.model,
        unit=price.currency, input_tokens=count, max_output_tokens=plan.max_output_tokens,
        upper_bound=upper, request=plan,
    )
