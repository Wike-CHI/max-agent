# Health, telemetry, and SLO notes

These targets are for a single-operator runtime over a 30 day window. They are not a hosted service agreement.

| Signal | Target | How to look |
| --- | --- | --- |
| Readiness success ratio | 0.99 of probes while the process is up | `GET /v1/ready` returns 200 and `database=ok` |
| Journal restore completeness | 1.0 (no lost events) | `scripts/restore_drill.py` reports `lost_events: []` |
| Unauthorized deploys | 0 | Production workflow has no deploy step; the gate refuses a missing CI pass or approval |

`/v1/health` is liveness: the process is up and reports its version. It does not touch the database.

`/v1/ready` runs `SELECT 1`. Failure is HTTP 503 with `detail` set to `database unavailable`. The body does not include the driver error, the database URL, or secret values.

`/v1/telemetry` returns in-process counters and the latest HTTP spans (`http.server.requests`, span name `http.request`). Spans store the method and path, not headers or the query string.

Set `MAX_AGENT_OTEL=1` to also open an OpenTelemetry API span when `opentelemetry` imports. No collector endpoint is configured in this repo, and a missing exporter does not fail the request. Economics stay on `GET /v1/metrics`; telemetry is the runtime hook, not the AER cost report.

Recovery time is operator-driven: restore the latest snapshot, then re-run the drill. There is no automatic failover.
