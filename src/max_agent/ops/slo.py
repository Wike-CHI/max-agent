"""Operator targets for a single-user task runtime.

These are review numbers for the runbook, not a hosted service agreement.
"""

from __future__ import annotations

# Share of readiness probes that succeed while the process is up, over WINDOW_DAYS.
READINESS_SUCCESS_RATIO = 0.99
# Every journal event present in the backup must be present after restore.
JOURNAL_RESTORE_COMPLETENESS = 1.0
# Deploys that skip CI or human approval. The workflow has no deploy step.
UNAUTHORIZED_DEPLOYS = 0
WINDOW_DAYS = 30
