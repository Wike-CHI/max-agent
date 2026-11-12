"""Post-CLI acceptance for document tasks. Does not invoke a coding CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_TERMINAL = tuple("。．.！!？?；;：:…」』\"'”’）)]`")


@dataclass
class DocumentCheck:
    ok: bool
    failure: str | None
    findings: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "failure": self.failure, "findings": list(self.findings)}


def looks_complete(text: str) -> bool:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    last = lines[-1].strip()
    if last.startswith(("#", "|")) or last in {"---", "***", "```"} or last.startswith("```"):
        return True
    return last.endswith(_TERMINAL)


def _heading_present(text: str, required: str) -> bool:
    want = required.strip()
    if not want:
        return True
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and want in stripped:
            return True
    return False


def evaluate_document_acceptance(root: Path, specs: list[dict[str, Any]]) -> DocumentCheck:
    """Check deliverable files: exist, non-empty, size/line bounds, headings, completeness."""
    findings: list[dict[str, Any]] = []
    first_failure: str | None = None
    for raw in specs:
        path = str(raw.get("path") or "").strip()
        item: dict[str, Any] = {"path": path}
        if not path or path.startswith("/") or ".." in Path(path).parts:
            item["failure"] = "document_path_invalid"
            findings.append(item)
            first_failure = first_failure or "document_path_invalid"
            continue
        target = (root / path).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            item["failure"] = "document_path_invalid"
            findings.append(item)
            first_failure = first_failure or "document_path_invalid"
            continue
        if not target.is_file():
            item["failure"] = "document_missing"
            findings.append(item)
            first_failure = first_failure or "document_missing"
            continue
        data = target.read_bytes()
        item["bytes"] = len(data)
        if len(data) == 0:
            item["failure"] = "document_empty"
            findings.append(item)
            first_failure = first_failure or "document_empty"
            continue
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines() or ([text] if text else [])
        item["lines"] = len(lines)
        min_bytes = int(raw.get("min_bytes") or 1)
        min_lines = int(raw.get("min_lines") or 1)
        max_lines = raw.get("max_lines")
        if len(data) < min_bytes or len(lines) < min_lines:
            item["failure"] = "document_too_short"
            findings.append(item)
            first_failure = first_failure or "document_too_short"
            continue
        if max_lines is not None and len(lines) > int(max_lines):
            item["failure"] = "document_too_long"
            findings.append(item)
            first_failure = first_failure or "document_too_long"
            continue
        required = [str(h) for h in (raw.get("required_headings") or []) if str(h).strip()]
        missing = [h for h in required if not _heading_present(text, h)]
        if missing:
            item["failure"] = "document_heading_missing"
            item["missing_headings"] = missing
            findings.append(item)
            first_failure = first_failure or "document_heading_missing"
            continue
        if raw.get("require_complete", True) and not looks_complete(text):
            item["failure"] = "document_incomplete"
            findings.append(item)
            first_failure = first_failure or "document_incomplete"
            continue
        item["ok"] = True
        findings.append(item)
    return DocumentCheck(ok=first_failure is None, failure=first_failure, findings=findings)


def acceptance_kind(payload: dict[str, Any]) -> str | None:
    block = payload.get("acceptance")
    if isinstance(block, dict) and block.get("kind"):
        kind = str(block["kind"]).strip().lower()
        if kind in {"document", "docs", "doc"}:
            return "document"
        if kind in {"code", "pytest", "tests"}:
            return "code"
        if kind in {"unverified", "none", "skip"}:
            return "unverified"
        return kind
    if payload.get("document_acceptance"):
        return "document"
    if payload.get("pytest_targets"):
        return "code"
    return None


def document_specs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    block = payload.get("acceptance") if isinstance(payload.get("acceptance"), dict) else {}
    if not block:
        block = payload.get("document_acceptance") if isinstance(payload.get("document_acceptance"), dict) else {}
    files = block.get("files") if isinstance(block, dict) else None
    if not files:
        return []
    return [item if isinstance(item, dict) else {"path": str(item)} for item in files]


def pytest_targets(workspace: Path, payload: dict[str, Any]) -> list[str] | None:
    raw = payload.get("pytest_targets")
    if not raw and isinstance(payload.get("acceptance"), dict):
        raw = payload["acceptance"].get("pytest_targets")
    if raw:
        if isinstance(raw, str):
            items = [part.strip() for part in raw.split(",") if part.strip()]
        else:
            items = [str(part).strip() for part in raw if str(part).strip()]
        return items or None
    fixture = workspace / "test_calc.py"
    if fixture.is_file():
        return ["test_calc.py"]
    return None


def missing_pytest_targets(workspace: Path, targets: list[str]) -> list[str]:
    missing: list[str] = []
    root = workspace.resolve()
    for raw in targets:
        rel = Path(raw)
        if not raw or rel.is_absolute() or ".." in rel.parts:
            missing.append(raw)
            continue
        target = (workspace / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            missing.append(raw)
            continue
        if not target.exists():
            missing.append(raw)
    return missing
