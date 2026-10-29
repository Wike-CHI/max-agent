#!/usr/bin/env bash
set -euo pipefail
cd /workspace/max-agent
export MAX_AGENT_DB_PATH=/workspace/max-agent/data/acceptance.db
rm -f "$MAX_AGENT_DB_PATH" "$MAX_AGENT_DB_PATH"-wal "$MAX_AGENT_DB_PATH"-shm

echo "=== 1. init-db ==="
uv run max-agent init-db

echo "=== 2. create mock task ==="
CREATE_OUT=$(uv run max-agent create "acceptance mock" \
  -c worker_ok -c 'output_equals:answer=42' \
  -w mock --payload '{"mode":"success","answer":"42"}' \
  -k accept-mock-1)
echo "$CREATE_OUT"
TID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$CREATE_OUT")
echo "TASK_ID=$TID"

echo "=== 3. run mock ==="
uv run max-agent run "$TID"

echo "=== 4. events ==="
uv run max-agent events "$TID" | head -c 4000
echo

echo "=== 5. local code task ==="
rm -f /tmp/max_agent_mvp_demo.py /tmp/test_max_agent_mvp_demo.py
CREATE2=$(uv run max-agent create "local code adder" \
  -c worker_ok -c 'file_exists:/tmp/max_agent_mvp_demo.py' \
  -w local_code \
  --payload '{"action":"write_and_pytest","path":"/tmp/max_agent_mvp_demo.py","test_path":"/tmp/test_max_agent_mvp_demo.py"}' \
  -k accept-local-1)
echo "$CREATE2"
TID2=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$CREATE2")
uv run max-agent run "$TID2"

echo "=== 6. simulate failure ==="
CREATE3=$(uv run max-agent create "fail then recover" \
  -c worker_ok \
  -w mock --payload '{"mode":"fail_recoverable"}' \
  --max-attempts 3 \
  -k accept-fail-1)
TID3=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$CREATE3")
uv run max-agent run "$TID3" || true
echo "(expect retry_wait)"
uv run max-agent get "$TID3"

echo "=== 7. restart simulation: new process query ==="
uv run python -c "
from max_agent.db.session import init_db, session_scope, reset_engine
from max_agent.kernel.service import TaskService
import os
reset_engine()
init_db()
with session_scope() as s:
    svc = TaskService(s)
    t = svc.get('$TID')
    print('after_restart_status=', t.status.value)
    print('events=', len(svc.list_events('$TID')))
    t2 = svc.get('$TID2')
    print('local_code_status=', t2.status.value)
"

echo "=== DONE ==="
