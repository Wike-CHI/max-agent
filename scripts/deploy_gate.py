"""Exit 0 only when the deploy gate is open. Never performs a deploy."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from max_agent.ops.deploy import DeployRefused, evaluate_deploy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check CI and human approval. Does not deploy.")
    parser.add_argument("--target", required=True, choices=["staging", "production"])
    parser.add_argument("--ci-passed", action="store_true")
    args = parser.parse_args(argv)
    try:
        decision = evaluate_deploy(target=args.target, ci_passed=args.ci_passed)
    except DeployRefused as exc:
        print(json.dumps({"proceed": False, "deploy_executed": False, "reason": str(exc)}))
        return 2
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
