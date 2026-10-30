"""Write SHA256SUMS and an unsigned provenance predicate for dist/.

A tag pipeline refuses to continue unless the tag is v{pyproject version}.
This does not sign artifacts and does not publish a release by itself.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from max_agent.ops.provenance import current_revision, package_version, require_release_tag, write_release_artifacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Checksum and provenance for wheel/sdist artifacts.")
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    args = parser.parse_args(argv)
    try:
        version = package_version(ROOT)
        ref = os.environ.get("GITHUB_REF", "")
        if ref.startswith("refs/tags/"):
            require_release_tag(os.environ.get("GITHUB_REF_NAME", ""), version=version)
        builder = "github-actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local"
        invocation = os.environ.get("GITHUB_RUN_ID", "local")
        written = write_release_artifacts(
            args.dist,
            version=version,
            revision=current_revision(),
            builder=builder,
            invocation_id=invocation,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"provenance: unsigned sha256 predicate ({written['attestation']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
