"""Read-only, deterministic route evaluation. Dispatch remains separately gated."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from max_agent.db.models import BudgetHoldRow, BudgetPolicyRow, ModelProfileRow, ProviderAccountRow, TaskRow
from max_agent.kernel.budget import _amount
from max_agent.kernel.registry import RegistryService

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
    ) -> RoutePreview:
        """No lease, attempt, hold, journal event or credential read is written."""
        instant = at.astimezone(timezone.utc) if at.tzinfo is not None else None
        if instant is None:
            raise ValueError("route preview time must include a timezone")
        amount = _amount(policy.upper_bound)
        if policy.vendor_pin is not None and policy.vendor_pin not in PROVIDER_VENDOR.values():
            raise ValueError("unsupported vendor pin")
        if any(not isinstance(label, str) or not label.strip() for label in policy.capabilities):
            raise ValueError("invalid capability")
        gates = worker_gates or {}
        bindings = model_binding_gates or {}
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
                if not model.enabled:
                    reasons.append("model_disabled")
                if not set(policy.capabilities).issubset(set(model.capabilities or [])):
                    reasons.append("capability_missing")
                if policy.effort and policy.effort not in (model.supported_efforts or []):
                    reasons.append("effort_unsupported")
                if policy.vendor_pin and vendor != policy.vendor_pin:
                    reasons.append("vendor_pin_mismatch")
                if vendor is None or not gates.get(vendor, False):
                    reasons.append("worker_gate_closed")
                if vendor is None or not bindings.get(vendor, False):
                    reasons.append("model_binding_unavailable")
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
