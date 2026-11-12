"""Test package.

Making `tests` a package keeps the repo root on `sys.path` under pytest's
default `prepend` import mode. That is what lets the new
`tests/test_document_acceptance.py` import the shared helpers from
`tests/test_cli_worker.py` regardless of how pytest is invoked
(`pytest ...`, `python -m pytest ...`, `uv run --no-sync pytest ...`).
"""
