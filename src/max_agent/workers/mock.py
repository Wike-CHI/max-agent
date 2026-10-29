"""Mock worker for deterministic demos and tests."""

from __future__ import annotations

import time
from typing import Any

from max_agent.models.enums import WorkerKind
from max_agent.models.schemas import WorkerResult
from max_agent.workers.base import Worker, WorkerContext


class MockWorker(Worker):
    kind = WorkerKind.MOCK

    def capabilities(self) -> set[str]:
        return {"mock", "echo", "fail"}

    def run(self, ctx: WorkerContext) -> WorkerResult:
        mode = str(ctx.payload.get("mode", "success"))
        delay = float(ctx.payload.get("delay_seconds", 0))
        if delay > 0:
            time.sleep(min(delay, ctx.timeout_seconds))

        if ctx.on_tool_call:
            ctx.on_tool_call("mock.echo", {"goal": ctx.goal, "mode": mode})

        if mode == "fail_recoverable":
            return WorkerResult(
                ok=False,
                summary="mock recoverable failure",
                outputs={"mode": mode},
                tool_calls=[{"tool": "mock.echo", "args": {"mode": mode}}],
                recoverable=True,
                error="simulated transient error",
            )

        if mode == "fail_hard":
            return WorkerResult(
                ok=False,
                summary="mock hard failure",
                outputs={"mode": mode},
                tool_calls=[{"tool": "mock.echo", "args": {"mode": mode}}],
                recoverable=False,
                error="simulated permanent error",
            )

        if mode == "fail_acceptance":
            # Worker claims success but outputs won't pass criteria
            return WorkerResult(
                ok=True,
                summary="mock said done but wrong output",
                outputs={"mode": mode, "answer": "wrong"},
                tool_calls=[{"tool": "mock.echo", "args": {"mode": mode}}],
                recoverable=True,
            )

        answer = ctx.payload.get("answer", "ok")
        outputs: dict[str, Any] = {"mode": mode, "answer": answer, "goal": ctx.goal}
        # echo expected keys for tests
        for k, v in ctx.payload.items():
            if k not in outputs:
                outputs[k] = v

        return WorkerResult(
            ok=True,
            summary=f"mock completed: {ctx.goal}",
            outputs=outputs,
            tool_calls=[{"tool": "mock.echo", "args": {"mode": mode}}],
            recoverable=True,
        )
