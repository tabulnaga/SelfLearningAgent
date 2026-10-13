"""Structured JSON logging. Context fields (request_id, session_id, ...) ride on contextvars, so every
log line inside a request is correlated without threading IDs through every call."""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.security.redaction import redact_obj

_ctx: ContextVar[dict[str, Any] | None] = ContextVar("log_ctx", default=None)

_SECRET_KEYS = {"api_key", "anthropic_api_key", "hindsight_api_key", "authorization", "password", "x-api-key"}


def bind(**fields: Any) -> None:
    _ctx.set({**(_ctx.get() or {}), **{k: v for k, v in fields.items() if v is not None}})


def clear() -> None:
    _ctx.set(None)


def current() -> dict[str, Any]:
    return dict(_ctx.get() or {})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **current(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        for key in list(payload):
            if key.lower() in _SECRET_KEYS:
                payload[key] = "<redacted>"
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(redact_obj(payload), default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel("WARNING")


def log_event(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": fields})
