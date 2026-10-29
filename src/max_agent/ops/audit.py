"""Offline audit of uv.lock: PyPI artifacts must carry sha256 digests."""

from __future__ import annotations

import tomllib

PYPI = "https://pypi.org/simple"


def _artifact_errors(name: str, artifact: object) -> list[str]:
    if not isinstance(artifact, dict):
        return [f"{name}: artifact is not a table"]
    digest = str(artifact.get("hash", ""))
    if not digest.startswith("sha256:") or len(digest) != len("sha256:") + 64:
        return [f"{name}: missing sha256"]
    return []


def audit_lock_text(text: str) -> list[str]:
    data = tomllib.loads(text)
    errors: list[str] = []
    for package in data.get("package", []):
        name = str(package.get("name", "?"))
        source = package.get("source") or {}
        if not isinstance(source, dict):
            errors.append(f"{name}: invalid source")
            continue
        if "editable" in source:
            continue
        if "git" in source or "url" in source or "path" in source:
            errors.append(f"{name}: non-pypi source {source.get('git') or source.get('url') or source.get('path')}")
            continue
        if source.get("registry") != PYPI:
            errors.append(f"{name}: non-pypi source {source}")
            continue
        artifacts: list[object] = []
        if "sdist" in package:
            artifacts.append(package["sdist"])
        artifacts.extend(package.get("wheels") or [])
        if not artifacts:
            errors.append(f"{name}: no artifacts")
            continue
        for artifact in artifacts:
            errors.extend(_artifact_errors(name, artifact))
    return errors
