"""In-process traces and counters, with an optional OpenTelemetry API bridge.

No collector is required. `MAX_AGENT_OTEL=1` starts API spans when the API
package imports; missing or broken exporters do not fail the request.
"""

from __future__ import annotations

import os


class _Span:
    def __init__(self, owner: "Telemetry", name: str, attributes: dict[str, object]) -> None:
        self.owner = owner
        self.name = name
        self.attributes = dict(attributes)
        self._otel: object | None = None

    def set(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def __enter__(self) -> "_Span":
        self._otel = _start_otel(self.name)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        status = "error" if exc_type else "ok"
        self.owner.remember(self.name, status, dict(self.attributes))
        otel = self._otel
        if otel is not None:
            try:
                otel.__exit__(exc_type, exc, tb)  # type: ignore[attr-defined]
            except Exception:
                pass
        return False


class Telemetry:
    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.spans: list[dict[str, object]] = []
        self.max_spans = 100

    def span(self, name: str, **attributes: object) -> _Span:
        return _Span(self, name, attributes)

    def counter(self, name: str, value: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + value

    def remember(self, name: str, status: str, attributes: dict[str, object]) -> None:
        self.spans.append({"name": name, "status": status, "attributes": attributes})
        if len(self.spans) > self.max_spans:
            self.spans = self.spans[-self.max_spans :]

    def snapshot(self) -> dict[str, object]:
        return {"counters": dict(self.counters), "spans": list(self.spans)}


_telemetry = Telemetry()


def get_telemetry() -> Telemetry:
    return _telemetry


def reset_telemetry() -> None:
    global _telemetry
    _telemetry = Telemetry()


def _start_otel(name: str) -> object | None:
    if os.environ.get("MAX_AGENT_OTEL") != "1":
        return None
    try:
        from opentelemetry import trace

        context = trace.get_tracer("max-agent").start_as_current_span(name)
        context.__enter__()
        return context
    except Exception:
        return None

