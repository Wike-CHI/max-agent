"""Read-only HTTP projections of provider accounts, models and price versions."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ProviderAccountRead(BaseModel):
    id: str
    account_key: str
    provider_key: str
    billing_mode: str
    enabled: bool

    model_config = ConfigDict(from_attributes=True)


class ModelProfileRead(BaseModel):
    id: str
    provider_key: str
    model_key: str
    capabilities: list[str]
    supported_efforts: list[str]
    context_window: int | None
    enabled: bool

    model_config = ConfigDict(from_attributes=True)


class PriceVersionRead(BaseModel):
    id: str
    account_id: str
    model_id: str
    effective_from: datetime
    pricing_unit: str
    currency: str
    input_rate: Decimal | None
    output_rate: Decimal | None
    cached_input_rate: Decimal | None
    compute_rate: Decimal | None
    source: str

    model_config = ConfigDict(from_attributes=True)
