#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

REAL=0
VENDOR=auto
while [[ $# -gt 0 ]]; do
  case "$1" in
    --real)
      REAL=1
      shift
      ;;
    --vendor)
      VENDOR="${2:-}"
      shift 2
      ;;
    *)
      echo "usage: $0 [--real] [--vendor auto|grok|codex|claude]" >&2
      exit 2
      ;;
  esac
done
case "$VENDOR" in
  auto|grok|codex|claude) ;;
  *)
    echo "unsupported vendor: $VENDOR" >&2
    exit 2
    ;;
esac

uv run --no-sync pytest -q tests/test_cli_worker.py \
  -k 'require_real_worker or fixture_issue_passes_with_stub'

if [[ "$REAL" -ne 1 ]]; then
  echo "PASS: require_real_worker gate tests passed; live external CLI not requested."
  echo "Run with --real --vendor claude or --real --vendor codex only after operator authorization."
  exit 0
fi

DRILL_DIR="$(mktemp -d /tmp/max-agent-real-cli-e2e.XXXXXX)"
export MAX_AGENT_DATABASE_URL="sqlite:///$DRILL_DIR/drill.db"
export MAX_AGENT_ALLOW_EXTERNAL_CLI=1
export MAX_AGENT_REQUIRE_REAL_WORKER=1

uv run --no-sync python -m max_agent.cli.main --json init-db > "$DRILL_DIR/init.json"
PAYLOAD="{\"vendor\":\"$VENDOR\",\"workspace_root\":\"$DRILL_DIR/workspaces\",\"require_real_worker\":true}"
uv run --no-sync python -m max_agent.cli.main --json create \
  'Real CLI E2E drill: implement the fixture issue' \
  -c worker_ok -c output_equals:pytest_exit_code=0 \
  -w cli --payload "$PAYLOAD" -k "real-cli-e2e-$VENDOR-$(date +%s)" --timeout 120 \
  > "$DRILL_DIR/create.json"
TASK_ID="$(uv run --no-sync python -c 'import json, sys; print(json.load(sys.stdin)["task_id"])' < "$DRILL_DIR/create.json")"
if ! uv run --no-sync python -m max_agent.cli.main --json run "$TASK_ID" > "$DRILL_DIR/run.json"; then
  echo "FAIL: CLI run command failed; evidence: $DRILL_DIR" >&2
  exit 1
fi
uv run --no-sync python -m max_agent.cli.main --json status "$TASK_ID" > "$DRILL_DIR/status.json"
uv run --no-sync python -m max_agent.cli.main --json events "$TASK_ID" > "$DRILL_DIR/events.json"

set +e
uv run --no-sync python - "$DRILL_DIR" "$TASK_ID" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
task_id = sys.argv[2]
status_body = json.loads((root / "status.json").read_text(encoding="utf-8"))
events_body = json.loads((root / "events.json").read_text(encoding="utf-8"))
outputs = {}
for event in reversed(events_body.get("events") or []):
    if event.get("event_type") == "worker_output":
        outputs = (event.get("payload") or {}).get("outputs") or {}
        break
summary = {
    "task_id": task_id,
    "status": status_body.get("status"),
    "verification": status_body.get("verification"),
    "vendor": outputs.get("vendor"),
    "adapter_failure": outputs.get("adapter_failure"),
    "cli_invoked": outputs.get("cli_invoked"),
    "real_cli_invoked": outputs.get("real_cli_invoked"),
    "require_real_worker": outputs.get("require_real_worker"),
    "probes": outputs.get("probes"),
    "artifact_manifest": outputs.get("artifact_manifest"),
}
(root / "real-cli-result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
print(json.dumps(summary, indent=2))
print(f"Evidence directory: {root}")

real = outputs.get("real_cli_invoked") is True
succeeded = summary["status"] == "succeeded"
verified = (summary["verification"] or {}).get("passed") is True
if succeeded and not real:
    print("FAIL: terminal succeeded without real_cli_invoked=true", file=sys.stderr)
    raise SystemExit(3)
if succeeded and real and verified:
    print("PASS: real CLI invoked, verification passed, and task succeeded.")
    raise SystemExit(0)
if not real and outputs.get("adapter_failure") in {
    "cli_not_authorized",
    "cli_unavailable",
    "real_worker_required",
}:
    print("SKIP: no real external CLI ran; inspect probes and adapter_failure.")
    raise SystemExit(0)
print("FAIL: real CLI drill did not meet its acceptance checks.", file=sys.stderr)
raise SystemExit(1)
PY
RESULT=$?
set -e
exit "$RESULT"
