"""Staging smoke and the production approval gate. The gate never deploys."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from max_agent.ops.deploy import DeployRefused, evaluate_deploy
from max_agent.ops.merge_gate import MergeRefused, evaluate_merge
from max_agent.ops.smoke import run_smoke

ROOT = Path(__file__).resolve().parents[1]


def test_staging_smoke_checks_health_ready_and_task(tmp_path: Path) -> None:
    report = run_smoke(database_url=f"sqlite:///{tmp_path / 'smoke.db'}")
    assert report["health"] == 200
    assert report["ready"] == 200
    assert report["task_id"]
    assert report["deploy_executed"] is False


def test_staging_requires_ci_and_does_not_deploy() -> None:
    with pytest.raises(DeployRefused, match="ci has not passed"):
        evaluate_deploy(target="staging", ci_passed=False)
    decision = evaluate_deploy(target="staging", ci_passed=True)
    assert decision["proceed"] is True
    assert decision["deploy_executed"] is False
    assert decision["target"] == "staging"


def test_production_requires_ci_and_human_approval_and_does_not_deploy() -> None:
    with pytest.raises(DeployRefused, match="ci has not passed"):
        evaluate_deploy(target="production", ci_passed=False, approval="approved-by:Wike-CHI")
    with pytest.raises(DeployRefused, match="human approval"):
        evaluate_deploy(target="production", ci_passed=True, approval="")
    with pytest.raises(DeployRefused, match="human approval"):
        evaluate_deploy(target="production", ci_passed=True, approval="APPROVE")
    decision = evaluate_deploy(
        target="production",
        ci_passed=True,
        approval="approved-by:Wike-CHI",
    )
    assert decision["proceed"] is True
    assert decision["deploy_executed"] is False
    assert decision["approver"] == "Wike-CHI"


def test_merge_gate_requires_green_checks_and_latest_approval() -> None:
    payload = {
        "reviews": [
            {"author": {"login": "Wike-CHI"}, "state": "APPROVED"},
            {"author": {"login": "Wike-CHI"}, "state": "CHANGES_REQUESTED"},
        ],
        "statusCheckRollup": [
            {"name": "test-py3.12", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
    }
    with pytest.raises(MergeRefused, match="human approval"):
        evaluate_merge(payload, target="master")

    payload["reviews"].append({"author": {"login": "reviewer"}, "state": "APPROVED"})
    payload["statusCheckRollup"].append(
        {"name": "test-py3.13", "status": "IN_PROGRESS", "conclusion": ""}
    )
    with pytest.raises(MergeRefused, match="ci has not passed"):
        evaluate_merge(payload, target="master")

    payload["statusCheckRollup"][1] = {
        "name": "test-py3.13",
        "status": "COMPLETED",
        "conclusion": "SUCCESS",
    }
    decision = evaluate_merge(payload, target="master")
    assert decision["proceed"] is True
    assert decision["merged"] is False


def test_deploy_workflow_has_no_deploy_step() -> None:
    text = (ROOT / ".github/workflows/deploy.yml").read_text()
    assert "environment: production" in text
    assert "inputs.approval == 'APPROVE'" in text
    assert "contents: write" not in text
    lowered = text.lower()
    for forbidden in ("kubectl", "terraform apply", "ssh ", "fly deploy", "serverless deploy"):
        assert forbidden not in lowered
