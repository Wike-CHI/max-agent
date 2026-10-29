"""Verify worker 'done' against acceptance criteria."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from max_agent.models.schemas import VerificationResult, WorkerResult


def verify(
    acceptance_criteria: list[str],
    worker_result: WorkerResult,
    payload: dict[str, Any] | None = None,
) -> VerificationResult:
    """MVP verifier.

    Supported criterion forms:
    - plain text: substring match against worker summary / outputs JSON string
    - file_exists:<path>
    - output_key:<key>
    - output_equals:<key>=<value>
    - regex:<pattern>  (search in summary)
    - worker_ok        (worker_result.ok must be true)
    """
    payload = payload or {}
    blob = _blob(worker_result)
    checks: list[dict[str, Any]] = []
    all_passed = True

    criteria = list(acceptance_criteria) if acceptance_criteria else ["worker_ok"]

    for crit in criteria:
        ok, detail = _check_one(crit, worker_result, blob, payload)
        checks.append({"criterion": crit, "passed": ok, "detail": detail})
        if not ok:
            all_passed = False

    msg = "all checks passed" if all_passed else "one or more checks failed"
    return VerificationResult(passed=all_passed, checks=checks, message=msg)


def _blob(result: WorkerResult) -> str:
    parts = [result.summary or "", result.error or ""]
    parts.append(str(result.outputs))
    return "\n".join(parts)


def _check_one(
    crit: str,
    result: WorkerResult,
    blob: str,
    payload: dict[str, Any],
) -> tuple[bool, str]:
    c = crit.strip()
    if c == "worker_ok":
        return result.ok, f"worker.ok={result.ok}"

    if c.startswith("file_exists:"):
        path = Path(c.split(":", 1)[1].strip())
        # allow path from worker outputs
        if "path" in result.outputs:
            path = Path(str(result.outputs["path"]))
        exists = path.is_file()
        return exists, f"path={path} exists={exists}"

    if c.startswith("output_key:"):
        key = c.split(":", 1)[1].strip()
        present = key in result.outputs
        return present, f"key={key} present={present}"

    if c.startswith("output_equals:"):
        rest = c.split(":", 1)[1]
        if "=" not in rest:
            return False, "malformed output_equals"
        key, expected = rest.split("=", 1)
        actual = result.outputs.get(key.strip())
        ok = str(actual) == expected
        return ok, f"key={key.strip()} actual={actual!r} expected={expected!r}"

    if c.startswith("regex:"):
        pattern = c.split(":", 1)[1]
        matched = re.search(pattern, blob) is not None
        return matched, f"regex matched={matched}"

    # default: substring
    found = c.lower() in blob.lower()
    return found, f"substring found={found}"
