"""Decide whether a human may proceed. This module never deploys."""

from __future__ import annotations

import os
import re

_APPROVER = re.compile(r"^approved-by:([A-Za-z0-9][A-Za-z0-9-]{0,38})$")


class DeployRefused(RuntimeError):
    pass


def evaluate_deploy(*, target: str, ci_passed: bool, approval: str | None = None) -> dict[str, object]:
    if target not in {"staging", "production"}:
        raise DeployRefused(f"unknown target {target}")
    if not ci_passed:
        raise DeployRefused("ci has not passed")
    approver = ""
    if target == "production":
        raw = os.environ.get("MAX_AGENT_DEPLOY_APPROVAL", "") if approval is None else approval
        match = _APPROVER.fullmatch(raw.strip())
        if match is None:
            raise DeployRefused("production requires human approval")
        approver = match.group(1)
    return {
        "proceed": True,
        "deploy_executed": False,
        "target": target,
        "approver": approver,
        "reason": "gate open; this workflow does not deploy",
    }
