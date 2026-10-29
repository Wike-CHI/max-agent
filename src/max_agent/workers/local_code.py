"""Safe local coding worker — runs bounded local shell/pytest only.

No network side effects, no paid APIs. Intended for MVP demos such as
writing a small file and verifying via pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from max_agent.models.enums import WorkerKind
from max_agent.models.schemas import WorkerResult
from max_agent.workers.base import Worker, WorkerContext
from max_agent.workers.sandbox import child_env, path_is_allowed, run_bounded, secret_values

# Path checks are an allowlist, not a sandbox. Pytest still runs as the host user.
_LOCAL_ROOTS = (Path("/tmp"), Path("/workspace"))


class LocalCodeWorker(Worker):
    kind = WorkerKind.LOCAL_CODE

    def capabilities(self) -> set[str]:
        return {"local_code", "pytest", "write_file"}

    def run(self, ctx: WorkerContext) -> WorkerResult:
        action = str(ctx.payload.get("action", "write_and_pytest"))
        tool_calls: list[dict[str, Any]] = []

        def note(tool: str, args: dict[str, Any]) -> None:
            tool_calls.append({"tool": tool, "args": args})
            if ctx.on_tool_call:
                ctx.on_tool_call(tool, args)

        if action == "write_file":
            return self._write_file(ctx, note, tool_calls)

        if action == "run_pytest":
            return self._run_pytest(ctx, note, tool_calls)

        if action == "write_and_pytest":
            wr = self._write_file(ctx, note, tool_calls)
            if not wr.ok:
                return wr
            pr = self._run_pytest(ctx, note, tool_calls)
            outputs = {**wr.outputs, **pr.outputs}
            return WorkerResult(
                ok=pr.ok,
                summary=f"write+pytest: {pr.summary}",
                outputs=outputs,
                tool_calls=tool_calls,
                recoverable=pr.recoverable,
                error=pr.error,
            )

        if action == "fail":
            note("local_code.fail", {})
            return WorkerResult(
                ok=False,
                summary="local_code forced failure",
                outputs={"action": action},
                tool_calls=tool_calls,
                recoverable=bool(ctx.payload.get("recoverable", True)),
                error="forced failure",
            )

        return WorkerResult(
            ok=False,
            summary=f"unknown action: {action}",
            outputs={},
            tool_calls=tool_calls,
            recoverable=False,
            error=f"unknown action: {action}",
        )

    def _write_file(
        self,
        ctx: WorkerContext,
        note: Any,
        tool_calls: list[dict[str, Any]],
    ) -> WorkerResult:
        path = Path(str(ctx.payload.get("path", "/tmp/max_agent_demo.py")))
        resolved = path.resolve()
        if not path_is_allowed(resolved, _LOCAL_ROOTS):
            return WorkerResult(
                ok=False,
                summary="path not allowed",
                outputs={"adapter_failure": "path_violation"},
                tool_calls=tool_calls,
                recoverable=False,
                error=f"refusing to write outside /workspace or /tmp: {resolved}",
            )

        content = ctx.payload.get("content")
        if content is None:
            # default demo: tiny adder module
            content = textwrap.dedent(
                '''\
                """Demo module written by LocalCodeWorker."""

                def add(a: int, b: int) -> int:
                    return a + b
                '''
            )

        note("local_code.write_file", {"path": str(resolved)})
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(str(content), encoding="utf-8")
        return WorkerResult(
            ok=True,
            summary=f"wrote {resolved}",
            outputs={"path": str(resolved), "bytes": resolved.stat().st_size},
            tool_calls=tool_calls,
        )

    def _run_pytest(
        self,
        ctx: WorkerContext,
        note: Any,
        tool_calls: list[dict[str, Any]],
    ) -> WorkerResult:
        test_path = Path(str(ctx.payload.get("test_path", "/tmp/test_max_agent_demo.py")))
        target = Path(str(ctx.payload.get("path", "/tmp/max_agent_demo.py")))
        resolved_test = test_path.resolve()
        resolved_target = target.resolve()
        if not path_is_allowed(resolved_test, _LOCAL_ROOTS):
            return self._reject_path(resolved_test, tool_calls)
        if not path_is_allowed(resolved_target, _LOCAL_ROOTS):
            return self._reject_path(resolved_target, tool_calls)

        if not resolved_test.exists():
            test_content = ctx.payload.get("test_content")
            if test_content is None:
                test_content = textwrap.dedent(
                    f'''\
                    import importlib.util
                    from pathlib import Path

                    def test_add():
                        path = Path({str(resolved_target)!r})
                        spec = importlib.util.spec_from_file_location("demo", path)
                        mod = importlib.util.module_from_spec(spec)
                        assert spec.loader is not None
                        spec.loader.exec_module(mod)
                        assert mod.add(2, 3) == 5
                    '''
                )
            note("local_code.write_test", {"path": str(resolved_test)})
            resolved_test.parent.mkdir(parents=True, exist_ok=True)
            resolved_test.write_text(str(test_content), encoding="utf-8")

        note("local_code.pytest", {"test_path": str(resolved_test)})
        home = Path(tempfile.mkdtemp(prefix="max-agent-pytest-home-", dir="/tmp"))
        tmp = Path(tempfile.mkdtemp(prefix="max-agent-pytest-tmp-", dir="/tmp"))
        ran = run_bounded(
            [sys.executable, "-m", "pytest", "-q", "--noconftest", str(resolved_test)],
            cwd=resolved_test.parent if path_is_allowed(resolved_test.parent, _LOCAL_ROOTS) else Path("/tmp"),
            env=child_env(home=home, tmp=tmp),
            timeout_seconds=min(ctx.timeout_seconds, 120),
            secrets=secret_values(os.environ),
        )
        if ran.timed_out:
            return WorkerResult(
                ok=False,
                summary="pytest timed out",
                outputs={"test_path": str(resolved_test), "adapter_failure": "timeout"},
                tool_calls=tool_calls,
                recoverable=True,
                error="timeout",
            )
        ok = ran.exit_code == 0
        return WorkerResult(
            ok=ok,
            summary=("pytest passed" if ok else "pytest failed"),
            outputs={
                "test_path": str(resolved_test),
                "returncode": ran.exit_code,
                "stdout": ran.stdout[-2000:],
                "stderr": ran.stderr[-2000:],
            },
            tool_calls=tool_calls,
            recoverable=True,
            error=None if ok else (ran.stderr[-500:] or ran.stdout[-500:]),
        )

    def _reject_path(self, resolved: Path, tool_calls: list[dict[str, Any]]) -> WorkerResult:
        return WorkerResult(
            ok=False,
            summary="test path not allowed",
            outputs={"adapter_failure": "path_violation"},
            tool_calls=tool_calls,
            recoverable=False,
            error=f"refusing test path: {resolved}",
        )
