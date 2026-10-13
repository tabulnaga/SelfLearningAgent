"""Worker process: SessionMonitor (time-based loop) + MemoryJobRunner (outbox loop).

    python -m workers
Both loops are independent, crash-isolated per iteration, and safe to run as several replicas.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from app.config import get_settings
from app.container import build_container
from app.observability.logging import setup_logging
from workers.memory_jobs import MemoryJobRunner
from workers.session_monitor import SessionMonitor

log = logging.getLogger("worker")


async def _loop(name: str, interval: float, fn, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await fn()
        except Exception:
            log.exception(f"{name} iteration failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    c = await build_container(settings)
    monitor = SessionMonitor(c.store, settings.session_timeout_minutes)
    runner = MemoryJobRunner(c.pool, c.store, c.memory, c.llm, settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    log.info("worker started", extra={"fields": {"timeout_minutes": settings.session_timeout_minutes,
                                                  "monitor_interval_s": settings.monitor_interval_s}})
    await asyncio.gather(
        _loop("session_monitor", settings.monitor_interval_s, monitor.run_once, stop),
        _loop("memory_jobs", settings.memory_job_interval_s, runner.run_once, stop),
    )


if __name__ == "__main__":
    asyncio.run(main())
