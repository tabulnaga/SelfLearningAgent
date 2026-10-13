"""Per-turn decision trace (spec §18 traceability: question → retrieval → memory → reasoning → tools →
answer → retained outcome). Every orchestrator step records into a TurnTrace; it is persisted as one
row in turn_traces and each step is also emitted as a log line."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any

from app.observability.logging import log_event

log = logging.getLogger("agent.trace")


class TurnTrace:
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.steps: list[dict[str, Any]] = []
        self.data: dict[str, Any] = {"request_id": request_id}
        self.llm_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        self._t0 = time.perf_counter()

    @contextmanager
    def step(self, name: str, **fields: Any):
        rec: dict[str, Any] = {"step": name, **fields}
        t = time.perf_counter()
        try:
            yield rec
            rec.setdefault("status", "ok")
        except Exception as e:
            rec["status"] = "error"
            rec["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            rec["latency_ms"] = round((time.perf_counter() - t) * 1000, 1)
            self.steps.append(rec)
            log_event(log, f"step:{name}", **rec)

    def add_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.llm_usage["input_tokens"] += input_tokens
        self.llm_usage["output_tokens"] += output_tokens
        self.llm_usage["calls"] += 1

    def set(self, **fields: Any) -> None:
        self.data.update(fields)

    def to_json(self) -> dict[str, Any]:
        return {
            **self.data,
            "steps": self.steps,
            "llm_usage": self.llm_usage,
            "total_latency_ms": round((time.perf_counter() - self._t0) * 1000, 1),
        }
