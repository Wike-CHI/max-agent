# Release artifacts

Package version lives in `pyproject.toml` (`project.version`), `src/max_agent/__init__.py` (`__version__`), and `uv.lock`. They must match. A GitHub Release tag must be exactly `v` plus that version. The current package version is **0.5.0**, prepared for a **pre-release**, not a production-grade v1.0.

## Local dry-run

The release workflow (`.github/workflows/release.yml`) runs tests, builds the wheel and sdist, then generates checksums and unsigned provenance. Reproduce that path without creating a tag or release:

```bash
uv sync --locked --extra dev
uv run --no-sync pytest -q
uv build
GITHUB_REF=refs/tags/v0.5.0 GITHUB_REF_NAME=v0.5.0 \
  uv run --no-sync python scripts/release_provenance.py --dist dist
(cd dist && sha256sum -c SHA256SUMS)
```

The tag environment variables only exercise the local consistency check. `release_provenance.py` rejects a tag that does not equal `v{project.version}`. It writes, next to the wheel and sdist:

- `SHA256SUMS` in `sha256sum` format
- `provenance.json`, an SLSA v1 predicate with the version, source revision, and artifact digests

`provenance.json` sets `attestation.signed` to false. It is not a Sigstore or GitHub artifact attestation.

## Human-approved pre-release publication

Do not create a public release unless a human has approved that exact tag. After approval and a green CI run on the intended commit, the human can run:

```bash
git tag -a v0.5.0 -m "MAX-Agent v0.5.0 pre-release"
git push origin v0.5.0
```

The tag push runs the verify job before publishing. The publish job uses `contents: write` and passes `--prerelease` for `v0.*` and hyphenated pre-release tags. A future stable `v1.0.0` tag is not marked as a pre-release, but the v1.0 exit criteria in `docs/ROADMAP.md` must pass first.

`workflow_dispatch` builds and uploads artifacts without publishing a GitHub Release. No production deploy runs from the release workflow.
