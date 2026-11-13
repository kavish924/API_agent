"""Structured JSON logging to stdout, with a run ID on every line (spec 00 F0.8)."""

from __future__ import annotations

import json
import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TextIO

_run_id: ContextVar[str | None] = ContextVar("guardian_run_id", default=None)


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def current_run_id() -> str | None:
    return _run_id.get()


@contextmanager
def run_context(run_id: str | None = None) -> Iterator[str]:
    """Bind a run ID to every log line emitted inside the block."""
    value = run_id or new_run_id()
    token = _run_id.set(value)
    try:
        yield value
    finally:
        _run_id.reset(token)


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Extra fields go in `extra={"fields": {...}}`."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "run_id": _run_id.get(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Send all logs to `stream` (stdout by default) as JSON lines."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
