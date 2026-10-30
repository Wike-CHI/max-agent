"""Checksums and an unsigned provenance predicate for wheel/sdist artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from pathlib import Path

PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
BUILD_TYPE = "https://github.com/Wike-CHI/max-agent/release-workflow@v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_version(root: Path) -> str:
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    from max_agent import __version__

    if declared != __version__:
        raise ValueError(f"version mismatch: pyproject {declared} != package {__version__}")
    return declared


def require_release_tag(tag: str, *, version: str) -> None:
    expected = f"v{version}"
    if tag != expected:
        raise ValueError(f"tag {tag} does not match package version {version} ({expected})")


def build_provenance(
    artifacts: list[Path],
    *,
    version: str,
    revision: str,
    builder: str,
    invocation_id: str,
) -> dict[str, object]:
    subjects = [
        {"name": path.name, "digest": {"sha256": sha256_file(path)}}
        for path in sorted(artifacts, key=lambda item: item.name)
    ]
    return {
        "predicateType": PREDICATE_TYPE,
        "subject": subjects,
        "predicate": {
            "buildDefinition": {
                "buildType": BUILD_TYPE,
                "externalParameters": {"version": version, "source": revision},
            },
            "runDetails": {
                "builder": {"id": builder},
                "metadata": {"invocationId": invocation_id},
            },
        },
        "attestation": {"signed": False, "kind": "sha256-predicate"},
    }


def write_release_artifacts(
    dist_dir: Path,
    *,
    version: str,
    revision: str,
    builder: str,
    invocation_id: str,
) -> dict[str, object]:
    artifacts = sorted(
        [path for path in dist_dir.iterdir() if path.suffix in {".whl", ".gz"} or path.name.endswith(".tar.gz")],
        key=lambda item: item.name,
    )
    # `.whl` suffix is `.whl`, sdist ends with `.tar.gz` (suffix `.gz`).
    artifacts = [path for path in artifacts if path.name.endswith(".whl") or path.name.endswith(".tar.gz")]
    if not artifacts:
        raise ValueError(f"no wheel or sdist in {dist_dir}")
    lines = [f"{sha256_file(path)}  {path.name}" for path in artifacts]
    (dist_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    document = build_provenance(
        artifacts,
        version=version,
        revision=revision,
        builder=builder,
        invocation_id=invocation_id,
    )
    (dist_dir / "provenance.json").write_text(json.dumps(document, indent=2) + "\n")
    return {
        "checksums": str(dist_dir / "SHA256SUMS"),
        "provenance": str(dist_dir / "provenance.json"),
        "attestation": {"signed": False, "kind": "sha256-predicate"},
    }


def current_revision() -> str:
    configured = os.environ.get("GITHUB_SHA", "").strip()
    if configured:
        return configured
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
