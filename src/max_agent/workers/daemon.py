"""Long-running queue worker with heartbeat, backoff, and graceful shutdown."""

from __future__ import annotations

import json
import logging
import signal
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timezone
from typing import TextIO

from max_agent.kernel.errors import ConcurrencyLimitError
from max_agent.kernel.scheduler import Scheduler
from max_agent.models.schemas import TaskRead
from max_agent.ops.secrets import install_secret_filter
from max_agent.ops.telemetry import get_telemetry

logger = logging.getLogger(__name__)

_LOG_FIELDS = (
    "event",
    "worker_id",
    "task_id",
    "attempt_id",
    "status",
    "concurrency",
    "active_tasks",
    "poll_interval_seconds",
    "heartbeat_interval_seconds",
    "lease_ttl_seconds",
    "delay_seconds",
    "signal",
    "error_type",
    "counters",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in _LOG_FIELDS:
            if hasattr(record, name):
                payload[name] = getattr(record, name)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_worker_logging(stream: TextIO | None = None) -> None:
    install_secret_filter()
    root = logging.getLogger("max_agent")
    root.setLevel(logging.INFO)
    root.propagate = False
    for handler in root.handlers:
        if getattr(handler, "_max_agent_worker_handler", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler._max_agent_worker_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)


class WorkerDaemon:
    def __init__(
        self,
        scheduler: Scheduler,
        *,
        concurrency: int = 1,
        poll_interval_seconds: float = 1.0,
        max_poll_interval_seconds: float = 30.0,
        error_backoff_cap_seconds: float = 30.0,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0")
        if max_poll_interval_seconds < poll_interval_seconds:
            raise ValueError("max_poll_interval_seconds must be >= poll_interval_seconds")
        if error_backoff_cap_seconds < poll_interval_seconds:
            raise ValueError("error_backoff_cap_seconds must be >= poll_interval_seconds")
        self.scheduler = scheduler
        self.concurrency = concurrency
        self.poll_interval_seconds = poll_interval_seconds
        self.max_poll_interval_seconds = max_poll_interval_seconds
        self.error_backoff_cap_seconds = error_backoff_cap_seconds
        self._stopped = threading.Event()

    def request_stop(self, signum: int | None = None, _frame: object | None = None) -> None:
        if not self._stopped.is_set():
            get_telemetry().counter("worker.daemon.shutdown_requested")
        self._stopped.set()
        logger.info(
            "worker shutdown requested",
            extra=self._fields(event="worker_shutdown_requested", signal=signum),
        )

    def run(self) -> None:
        previous_handlers = self._install_signal_handlers()
        idle_delay = self.poll_interval_seconds
        error_delay = self.poll_interval_seconds
        logger.info(
            "worker started",
            extra=self._fields(
                event="worker_started",
                concurrency=self.concurrency,
                poll_interval_seconds=self.poll_interval_seconds,
                heartbeat_interval_seconds=self.scheduler.heartbeat_interval_seconds,
                lease_ttl_seconds=self.scheduler.lease_ttl_seconds,
            ),
        )
        get_telemetry().counter("worker.daemon.started")
        try:
            with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="max-agent-worker") as pool:
                running: set[Future[TaskRead]] = set()
                while not self._stopped.is_set():
                    while len(running) < self.concurrency and not self._stopped.is_set():
                        running.add(pool.submit(self.scheduler.run_one))
                    done, pending = wait(
                        running,
                        timeout=self.poll_interval_seconds,
                        return_when=FIRST_COMPLETED,
                    )
                    running = set(pending)
                    outcome = self._process_futures(done)
                    if outcome == "work":
                        idle_delay = self.poll_interval_seconds
                        error_delay = self.poll_interval_seconds
                    elif outcome == "error":
                        idle_delay = self.poll_interval_seconds
                    elif outcome == "empty":
                        error_delay = self.poll_interval_seconds
                    if running or outcome not in {"empty", "error"}:
                        continue
                    delay = error_delay if outcome == "error" else idle_delay
                    logger.info(
                        "worker backoff",
                        extra=self._fields(event=f"worker_{outcome}_backoff", delay_seconds=delay),
                    )
                    self._stopped.wait(delay)
                    if outcome == "error":
                        error_delay = min(self.error_backoff_cap_seconds, error_delay * 2)
                    else:
                        idle_delay = min(self.max_poll_interval_seconds, idle_delay * 2)
                if running:
                    logger.info(
                        "waiting for active tasks to finish",
                        extra=self._fields(event="worker_draining", active_tasks=len(running)),
                    )
                for future in as_completed(running):
                    self._process_futures({future})
        finally:
            self._restore_signal_handlers(previous_handlers)
            counters = get_telemetry().snapshot()["counters"]
            logger.info(
                "worker stopped",
                extra=self._fields(event="worker_stopped", counters=counters),
            )

    def _process_futures(self, futures: set[Future[TaskRead]]) -> str | None:
        outcome: str | None = None
        for future in futures:
            try:
                task = future.result()
            except LookupError:
                get_telemetry().counter("worker.queue.empty")
                outcome = outcome or "empty"
            except ConcurrencyLimitError:
                get_telemetry().counter("worker.queue.busy")
                outcome = outcome or "empty"
            except Exception as exc:
                get_telemetry().counter("worker.daemon.errors")
                logger.error(
                    "worker iteration failed: %s",
                    exc,
                    extra=self._fields(event="worker_iteration_error", error_type=type(exc).__name__),
                )
                outcome = "error"
            else:
                get_telemetry().counter("worker.tasks.finished")
                get_telemetry().counter(f"worker.tasks.{task.status.value}")
                logger.info(
                    "task %s finished with status %s",
                    task.id,
                    task.status.value,
                    extra=self._fields(event="task_finished", task_id=task.id, status=task.status.value),
                )
                outcome = "work"
        return outcome

    def _fields(self, **values: object) -> dict[str, object]:
        return {"worker_id": self.scheduler.worker_id, **values}

    def _install_signal_handlers(self) -> dict[signal.Signals, object]:
        if threading.current_thread() is not threading.main_thread():
            return {}
        previous: dict[signal.Signals, object] = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self.request_stop)
        return previous

    def _restore_signal_handlers(self, previous: dict[signal.Signals, object]) -> None:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
