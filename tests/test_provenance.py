"""Release artifact version, checksums, and provenance."""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

from max_agent import __version__
from max_agent.ops.provenance import build_provenance, require_release_tag, write_release_artifacts


def test_package_version_matches_pyproject() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__


def test_release_artifacts_have_checksums_and_provenance(tmp_path: Path) -> None:
    wheel = tmp_path / "max_agent-0.1.0-py3-none-any.whl"
    sdist = tmp_path / "max_agent-0.1.0.tar.gz"
    wheel.write_bytes(b"wheel-bytes")
    sdist.write_bytes(b"sdist-bytes")

    written = write_release_artifacts(
        tmp_path,
        version=__version__,
        revision="abc123",
        builder="test",
        invocation_id="local",
    )

    checksums = (tmp_path / "SHA256SUMS").read_text()
    assert f"{hashlib.sha256(b'wheel-bytes').hexdigest()}  {wheel.name}" in checksums
    assert f"{hashlib.sha256(b'sdist-bytes').hexdigest()}  {sdist.name}" in checksums

    provenance = json.loads((tmp_path / "provenance.json").read_text())
    assert provenance["predicateType"] == "https://slsa.dev/provenance/v1"
    names = {item["name"] for item in provenance["subject"]}
    assert wheel.name in names
    assert sdist.name in names
    assert provenance["predicate"]["buildDefinition"]["externalParameters"]["version"] == __version__
    assert provenance["predicate"]["buildDefinition"]["externalParameters"]["source"] == "abc123"
    assert "signed" in written["attestation"]
    assert written["attestation"]["signed"] is False


def test_tag_must_match_package_version() -> None:
    require_release_tag(f"v{__version__}", version=__version__)
    try:
        require_release_tag("v9.9.9", version=__version__)
    except ValueError as exc:
        assert __version__ in str(exc)
    else:
        raise AssertionError("mismatched tag was accepted")


def test_release_workflow_writes_provenance() -> None:
    text = Path(".github/workflows/release.yml").read_text()
    assert "scripts/release_provenance.py" in text


def test_build_provenance_subjects_use_sha256(tmp_path: Path) -> None:
    artifact = tmp_path / "demo.whl"
    artifact.write_bytes(b"abc")
    document = build_provenance(
        [artifact],
        version="0.1.0",
        revision="deadbeef",
        builder="test",
        invocation_id="1",
    )
    digest = document["subject"][0]["digest"]["sha256"]
    assert digest == hashlib.sha256(b"abc").hexdigest()
