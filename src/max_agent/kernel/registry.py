"""Owner-scoped provider accounts and immutable effective-dated prices."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from max_agent.db.models import ModelProfileRow, PriceVersionRow, ProviderAccountRow

_KEY = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*\Z")
_CREDENTIAL_REF = re.compile(r"(?:env|vault|secret):[A-Za-z0-9][A-Za-z0-9_./-]*\Z")
_MODES = {"api_metered", "subscription", "prepaid", "local_compute", "unknown"}


def _key(value: str, limit: int, *, model: bool = False) -> str:
    text = value.strip()
    if len(text) > limit or not (_MODEL if model else _KEY).fullmatch(text):
        raise ValueError("invalid registry key")
    return text


def _labels(values: Iterable[str]) -> list[str]:
    if isinstance(values, str):
        raise ValueError("labels must be an iterable of strings")
    return sorted({_key(value, 64, model=True) for value in values})


def _rate(value: Decimal | str | int | None) -> Decimal | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid price rate") from None
    if not amount.is_finite() or amount < 0 or amount.normalize().as_tuple().exponent < -18:
        raise ValueError("price rate must be nonnegative with at most 18 decimal places")
    if amount >= Decimal("1000000000000000000"):
        raise ValueError("price rate exceeds storage precision")
    return amount


def _when(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("effective_from must have a timezone")
    return value.astimezone(timezone.utc)


def _source(value: str) -> str:
    text = value.strip()
    if len(text) > 256:
        raise ValueError("invalid price source")
    if re.fullmatch(r"(?:manual|fixture):[A-Za-z0-9][A-Za-z0-9_./-]*", text):
        return text
    parsed = urlsplit(text)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("price source must be a safe HTTPS URL or manual:/fixture: reference")
    return text


class RegistryService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def register_account(
        self, *, owner: str, account_key: str, provider_key: str,
        billing_mode: str, credential_ref: str | None = None, enabled: bool = False,
    ) -> ProviderAccountRow:
        if billing_mode not in _MODES:
            raise ValueError("invalid billing mode")
        owner = owner.strip()
        if not owner or len(owner) > 128:
            raise ValueError("invalid account owner")
        if credential_ref is not None and (
            len(credential_ref) > 256 or not _CREDENTIAL_REF.fullmatch(credential_ref)
        ):
            raise ValueError("credential_ref must be an env:/vault:/secret: reference")
        account_key = _key(account_key, 64)
        if self.session.query(ProviderAccountRow).filter_by(owner=owner, account_key=account_key).first():
            raise ValueError("account key already exists")
        row = ProviderAccountRow(
            id=str(uuid.uuid4()), owner=owner, account_key=account_key,
            provider_key=_key(provider_key, 64), billing_mode=billing_mode,
            credential_ref=credential_ref, enabled=enabled,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def register_model(
        self, *, provider_key: str, model_key: str, capabilities: Iterable[str],
        supported_efforts: Iterable[str] = (), context_window: int | None = None,
        enabled: bool = False,
    ) -> ModelProfileRow:
        provider_key = _key(provider_key, 64)
        model_key = _key(model_key, 128, model=True)
        if context_window is not None and context_window < 1:
            raise ValueError("context_window must be positive")
        if self.session.query(ModelProfileRow).filter_by(provider_key=provider_key, model_key=model_key).first():
            raise ValueError("model profile already exists")
        row = ModelProfileRow(
            id=str(uuid.uuid4()), provider_key=provider_key, model_key=model_key,
            capabilities=_labels(capabilities), supported_efforts=_labels(supported_efforts),
            context_window=context_window, enabled=enabled,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def add_price(
        self, *, account_id: str, model_id: str, effective_from: datetime,
        pricing_unit: str, currency: str, source: str,
        input_rate: Decimal | str | int | None = None,
        output_rate: Decimal | str | int | None = None,
        cached_input_rate: Decimal | str | int | None = None,
        compute_rate: Decimal | str | int | None = None,
    ) -> PriceVersionRow:
        # Lock the account before checking the latest version. This serializes
        # concurrent writers even if the model has no price row yet.
        account = self.session.query(ProviderAccountRow).filter_by(id=account_id).with_for_update().one_or_none()
        model = self.session.get(ModelProfileRow, model_id)
        if account is None or model is None or account.provider_key != model.provider_key:
            raise ValueError("account and model must belong to the same provider")
        effective_from = _when(effective_from)
        pricing_unit = _key(pricing_unit, 32)
        currency = _key(currency.lower(), 16).upper()
        rates = [_rate(value) for value in (input_rate, output_rate, cached_input_rate, compute_rate)]
        if all(rate is None for rate in rates):
            raise ValueError("at least one rate is required")
        source = _source(source)
        latest = (
            self.session.query(PriceVersionRow)
            .filter_by(account_id=account_id, model_id=model_id)
            .order_by(PriceVersionRow.effective_from.desc())
            .first()
        )
        if latest is not None:
            current_start = latest.effective_from.replace(tzinfo=timezone.utc) if latest.effective_from.tzinfo is None else latest.effective_from
            if effective_from <= current_start:
                raise ValueError("price versions must be appended in effective-time order")
            if (pricing_unit, currency) != (latest.pricing_unit, latest.currency):
                raise ValueError("new price unit or currency requires a distinct account")
        row = PriceVersionRow(
            id=str(uuid.uuid4()), account_id=account_id, model_id=model_id,
            effective_from=effective_from, pricing_unit=pricing_unit, currency=currency,
            input_rate=rates[0], output_rate=rates[1], cached_input_rate=rates[2], compute_rate=rates[3],
            source=source,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def accounts(self, owner: str) -> list[ProviderAccountRow]:
        return self.session.query(ProviderAccountRow).filter_by(owner=owner).order_by(ProviderAccountRow.account_key).all()

    def models(self, provider_key: str | None = None) -> list[ModelProfileRow]:
        query = self.session.query(ModelProfileRow)
        if provider_key is not None:
            query = query.filter_by(provider_key=_key(provider_key, 64))
        return query.order_by(ModelProfileRow.provider_key, ModelProfileRow.model_key).all()

    def price_at(self, *, owner: str, account_id: str, model_id: str, at: datetime) -> PriceVersionRow | None:
        account = self.session.get(ProviderAccountRow, account_id)
        model = self.session.get(ModelProfileRow, model_id)
        if account is None or account.owner != owner or model is None or account.provider_key != model.provider_key:
            raise KeyError("account or model not found")
        instant = _when(at)
        versions = (
            self.session.query(PriceVersionRow)
            .filter_by(account_id=account_id, model_id=model_id)
            .order_by(PriceVersionRow.effective_from.desc())
            .all()
        )
        for version in versions:
            starts = version.effective_from.replace(tzinfo=timezone.utc) if version.effective_from.tzinfo is None else version.effective_from
            if starts <= instant:
                return version
        return None
