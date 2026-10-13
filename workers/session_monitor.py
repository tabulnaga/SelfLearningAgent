"""SessionMonitor (spec §10): a periodic sweep instead of a blocking wait inside any workflow.
Finds ACTIVE sessions idle longer than SESSION_TIMEOUT_MINUTES and closes them as TIMED_OUT; the same
transaction enqueues their memory job."""

from __future__ import annotations

import logging

from app.observability.logging import log_event
from app.sessions.store import SessionStore

log = logging.getLogger("worker.session_monitor")


class SessionMonitor:
    def __init__(self, store: SessionStore, timeout_minutes: int) -> None:
        self.store = store
        self.timeout_minutes = timeout_minutes

    async def run_once(self) -> list[str]:
        closed = await self.store.time_out_idle(self.timeout_minutes)
        if closed:
            log_event(log, "sessions timed out", count=len(closed), session_ids=closed,
                      timeout_minutes=self.timeout_minutes)
        return closed
