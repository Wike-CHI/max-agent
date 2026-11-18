"""Read-only, deterministic route evaluation. Dispatch remains separately gated."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import uuid

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from max_agent.db.models import (
    BudgetHoldRow, BudgetPolicyRow, ModelProfileRow, ProviderAccountRow,
    RouteDecisionRow, TaskRow,
)
from max_agent.kernel.budget import BudgetService, _amount
from max_agent.kernel.registry import RegistryService
from max_agent.kernel.journal import Journal
from max_agent.models.enums import EventType
from max_agent.models.route_contract import ROUTED_CREDENTIAL_ENV

PROVIDER_VENDOR = {"openai": "codex", "anthropic": "claude", "xai": "grok"}


class RoutePolicy(BaseModel):
    """An explicit upper bound, never an inferred bill or a currency conversion."""

    unit: str = Field(min_length=1, max_length=32)
    upper_bound: Decimal = Field(ge=0)
    capabilities: list[str] = Field(default_factory=list)
    effort: str | None = None
    vendor_pin: str | None = None


class RouteCandidate(BaseModel):
    account_id: str
    provider: str
    model_id: str
    model: str
    vendor: str | None
    price_version_id: str | None
    reason_codes: list[str]
    dispatchable: bool


class RoutePreview(BaseModel):
    task_id: str
    unit: str
    upper_bound: Decimal
    candidates: list[RouteCandidate]
    selected: RouteCandidate | None


class RouteService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def preview(
        self, *, task: TaskRow, policy: RoutePolicy, at: datetime,
        worker_gates: dict[str, bool] | None = None,
        model_binding_gates: dict[str, bool] | None = None,
        spend_cap_gates: dict[str, bool] | None = None,
    ) -> RoutePreview:
        """No lease, attempt, hold, journal event or credential read is written."""
        instant = at.astimezone(timezone.utc) if at.tzinfo is not None else None
        if instant is None:
            raise ValueError("route preview time must include a timezone")
        amount = _amount(policy.upper_bound)
        if policy.vendor_pin is not None and policy.vendor_pin not in PROVIDER_VENDOR.values():
            raise ValueError("unsupported vendor pin")
        payload_pin = (task.worker_payload or {}).get("vendor")
        if payload_pin not in (None, "auto", "stub", *PROVIDER_VENDOR.values()):
            raise ValueError("unsupported task vendor")
        if payload_pin == "stub":
            raise ValueError("stub cannot execute a paid route")
        if any(not isinstance(label, str) or not label.strip() for label in policy.capabilities):
            raise ValueError("invalid capability")
        gates = worker_gates or {}
        bindings = model_binding_gates or {}
        spend_caps = spend_cap_gates or {}
        accounts = self.session.query(ProviderAccountRow).filter_by(owner=task.created_by).order_by(
            ProviderAccountRow.provider_key, ProviderAccountRow.account_key
        ).all()
        registry = RegistryService(self.session)
        candidates: list[RouteCandidate] = []
        for account in accounts:
            vendor = PROVIDER_VENDOR.get(account.provider_key)
            models = self.session.query(ModelProfileRow).filter_by(provider_key=account.provider_key).order_by(
                ModelProfileRow.model_key
            ).all()
            for model in models:
                reasons: list[str] = []
                if not account.enabled:
                    reasons.append("account_disabled")
                if account.billing_mode != "api_metered":
                    reasons.append("billing_mode_unsupported")
                expected_env = ROUTED_CREDENTIAL_ENV.get(vendor or "")
                if expected_env is None or account.credential_ref != f"env:{expected_env}":
                    reasons.append("credential_binding_missing")
                if not model.enabled:
                    reasons.append("model_disabled")
                if not set(policy.capabilities).issubset(set(model.capabilities or [])):
                    reasons.append("capability_missing")
                if policy.effort and policy.effort not in (model.supported_efforts or []):
                    reasons.append("effort_unsupported")
                if policy.vendor_pin and vendor != policy.vendor_pin:
                    reasons.append("vendor_pin_mismatch")
                if payload_pin not in (None, "auto") and vendor != payload_pin:
                    reasons.append("task_vendor_pin_mismatch")
                if vendor is None or not gates.get(vendor, False):
                    reasons.append("worker_gate_closed")
                if vendor is None or not bindings.get(vendor, False):
                    reasons.append("model_binding_unavailable")
                if vendor is None or not spend_caps.get(vendor, False):
                    reasons.append("spend_ceiling_unenforced")
                price = registry.price_at(owner=task.created_by, account_id=account.id, model_id=model.id, at=instant)
                if price is None:
                    reasons.append("price_missing")
                elif not price.source.startswith("https://"):
                    reasons.append("price_untrusted")
                if price is not None and price.currency != policy.unit:
                    reasons.append("unit_mismatch")
                if not self._budget_available(task, account, policy.unit, amount, instant):
                    reasons.append("budget_unavailable")
                candidates.append(RouteCandidate(
                    account_id=account.id, provider=account.provider_key,
                    model_id=model.id, model=model.model_key, vendor=vendor,
                    price_version_id=price.id if price else None,
                    reason_codes=reasons, dispatchable=not reasons,
                ))
        return RoutePreview(
            task_id=task.id, unit=policy.unit, upper_bound=amount,
            candidates=candidates, selected=next((c for c in candidates if c.dispatchable), None),
        )

    def bind_attempt(
        self, *, task: TaskRow, attempt_id: str, fencing_token: int,
        policy: RoutePolicy, selected: RouteCandidate, at: datetime,
    ) -> RouteDecisionRow:
        """Called inside the claim transaction, after attempt insertion and before dispatch."""
        if not selected.dispatchable or not selected.vendor or not selected.price_version_id:
            raise ValueError("selected route is not dispatchable")
        account = self.session.query(ProviderAccountRow).filter_by(id=selected.account_id).with_for_update().one_or_none()
        model = self.session.query(ModelProfileRow).filter_by(id=selected.model_id).with_for_update().one_or_none()
        if (
            account is None or model is None or account.owner != task.created_by
            or not account.enabled or not model.enabled or account.provider_key != model.provider_key
            or selected.vendor != PROVIDER_VENDOR.get(account.provider_key)
            or account.billing_mode != "api_metered"
            or account.credential_ref != f"env:{ROUTED_CREDENTIAL_ENV.get(selected.vendor, '')}"
            or model.model_key != selected.model
            or not set(policy.capabilities).issubset(set(model.capabilities or []))
            or (policy.effort is not None and policy.effort not in (model.supported_efforts or []))
        ):
            raise ValueError("route account, model or capability changed during claim")
        price = RegistryService(self.session).price_at(
            owner=task.created_by, account_id=account.id, model_id=model.id, at=at,
        )
        if (
            price is None or price.id != selected.price_version_id
            or price.currency != policy.unit or not price.source.startswith("https://")
        ):
            raise ValueError("route price changed during claim")
        reservation = BudgetService(self.session).reserve(
            owner=task.created_by, account_id=selected.account_id, task_id=task.id,
            attempt_id=attempt_id, fencing_token=fencing_token, unit=policy.unit,
            upper_bound=policy.upper_bound, idempotency_key=f"route:{attempt_id}", now=at,
        )
        row = RouteDecisionRow(
            id=str(uuid.uuid4()), task_id=task.id, attempt_id=attempt_id,
            fencing_token=fencing_token, reservation_id=reservation.id,
            account_id=selected.account_id, model_id=selected.model_id,
            price_version_id=selected.price_version_id, policy_version="route.v1",
            vendor=selected.vendor, model_key=selected.model, effort=policy.effort,
            unit=policy.unit, upper_bound=_amount(policy.upper_bound),
            policy_snapshot=policy.model_dump(mode="json"),
            reason_codes=["selected"],
        )
        self.session.add(row)
        self.session.flush()
        Journal(self.session).append(task.id, EventType.ROUTE_SELECTED, "route selected and reserved", {
            "decision_id": row.id, "reservation_id": reservation.id,
            "attempt_id": attempt_id, "fencing_token": fencing_token,
            "model_id": model.id, "account_id": account.id,
            "price_version_id": price.id, "policy_version": row.policy_version,
            "unit": row.unit, "upper_bound": str(row.upper_bound),
        })
        return row

    def _budget_available(
        self, task: TaskRow, account: ProviderAccountRow, unit: str,
        amount: Decimal, instant: datetime,
    ) -> bool:
        for scope, scope_id in (("global", "all"), ("provider", account.provider_key), ("task", task.id)):
            policies = self.session.query(BudgetPolicyRow).filter_by(
                owner=task.created_by, scope=scope, scope_id=scope_id, unit=unit, enabled=True
            ).all()
            active = [p for p in policies if _utc(p.period_start) <= instant < _utc(p.period_end)]
            if len(active) != 1:
                return False
            committed = sum((h.committed for h in self.session.query(BudgetHoldRow).filter_by(
                policy_id=active[0].id
            ).all()), Decimal(0))
            if committed + amount > active[0].ceiling:
                return False
        return True


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
