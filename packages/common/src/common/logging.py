"""Structured JSON logging with request ID context propagation."""

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

_request_id: ContextVar[str | None] = ContextVar("telco_request_id", default=None)


@contextmanager
def bind_request_id(request_id: str) -> Iterator[None]:
    """Bind a request ID for log records in the current context."""

    token = _request_id.set(request_id)
    try:
        yield
    finally:
        _request_id.reset(token)


def get_request_id() -> str | None:
    """Return the request ID bound to the current context, if any."""

    return _request_id.get()


class JsonLogFormatter(logging.Formatter):
    """Format logs as one-line JSON with safe correlation fields."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z")
        request_id = getattr(record, "request_id", None) or get_request_id()
        payload: dict[str, object] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id,
        }
        for field in ("component", "error_code", "run_id"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_logging(level: int | str = logging.INFO) -> None:
    """Configure one JSON stream handler for the process root logger."""

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    root_logger.addHandler(handler)
    root_logger.setLevel(level)


__all__ = ["JsonLogFormatter", "bind_request_id", "configure_logging", "get_request_id"]
