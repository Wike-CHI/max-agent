"""Refuse a master merge unless CI is green and a review is APPROVED.

Reads a `gh pr view --json reviews,statusCheckRollup` payload from disk.
Does not call GitHub and does not merge.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from max_agent.ops.merge_gate import MergeRefused, evaluate_merge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local master merge gate. Does not merge.")
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--target", default="master")
    args = parser.parse_args(argv)
    payload = json.loads(args.payload.read_text())
    try:
        decision = evaluate_merge(payload, target=args.target)
    except MergeRefused as exc:
        print(json.dumps({"proceed": False, "merged": False, "reason": str(exc)}))
        return 2
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
