# Release artifacts

Package version lives in both `pyproject.toml` (`project.version`) and `src/max_agent/__init__.py` (`__version__`). They must match. A GitHub Release tag must be exactly `v` plus that version, for example `v0.1.0`.

The release workflow (`.github/workflows/release.yml`) runs tests, `uv build`, then:

```bash
uv run --no-sync python scripts/release_provenance.py --dist dist
```

That writes, next to the wheel and sdist:

- `SHA256SUMS` in `sha256sum` format
- `provenance.json`, an SLSA v1 predicate with the version, source revision, and artifact digests

`provenance.json` sets `attestation.signed` to false. It is not a Sigstore or GitHub artifact attestation. Check a download with:

```bash
sha256sum -c SHA256SUMS
```

The publish job uses `contents: write` and only runs for a `v*` tag after the verify job succeeds. `workflow_dispatch` builds artifacts and does not publish. Do not create a public release unless a human asked for that tag.

No production deploy runs from the release workflow.
