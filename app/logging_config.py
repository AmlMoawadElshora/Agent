"""Structured-ish logging setup (JSON friendly, no external dependency)."""

from __future__ import annotations

import logging
import sys


class _KeyValueFormatter(logging.Formatter):
    """Appends ``extra`` fields as ``key=value`` pairs.

    Keeps logs greppable in a terminal and still parseable when a log shipper
    wraps them, without pulling in structlog.
    """

    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in self._RESERVED and not key.startswith("_")
        }
        if not extras:
            return base
        rendered = " ".join(f"{key}={value}" for key, value in sorted(extras.items()))
        return f"{base} | {rendered}"


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if getattr(root, "_agent_configured", False):
        root.setLevel(level.upper())
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        _KeyValueFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Twilio's HTTP client is chatty at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("twilio").setLevel(logging.WARNING)
    root._agent_configured = True
