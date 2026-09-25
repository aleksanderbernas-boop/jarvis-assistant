"""Structured logging with secret redaction and per-request context.

Every record carries `session_id` / `task_id` from context variables, is
written as JSON lines to `data/logs/jarvis.log`, and is scrubbed of API keys
before it reaches any handler.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import re
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

session_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("session_id", default=None)
task_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("task_id", default=None)

# Common credential shapes, redacted even if they were never configured.
_SECRET_PATTERNS = [
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"tvly-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{6,}"),
]


class Redactor:
    def __init__(self) -> None:
        self._literals: list[str] = []

    def add_secrets(self, secrets: list[str]) -> None:
        self._literals = sorted({s for s in [*self._literals, *secrets] if len(s) >= 6}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for literal in self._literals:
            text = text.replace(literal, "[REDACTED]")
        for pattern in _SECRET_PATTERNS:
            if pattern.groups:
                text = pattern.sub(lambda m: m.group(1) + "[REDACTED]", text)
            else:
                text = pattern.sub("[REDACTED]", text)
        return text


redactor = Redactor()

# Recent warnings/errors, surfaced by diagnostics ("why did that fail?").
recent_errors: deque[dict[str, Any]] = deque(maxlen=50)

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = getattr(record, "session_id", None) or session_id_var.get()  # type: ignore[attr-defined]
        record.task_id = getattr(record, "task_id", None) or task_id_var.get()  # type: ignore[attr-defined]
        # Format first, then redact the final text (keeps %-formatting of numbers intact).
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            message = f"{record.msg} {record.args}"
        record.msg, record.args = redactor.redact(message), None
        if record.levelno >= logging.WARNING:
            recent_errors.append(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage()[:500],
                    "session_id": getattr(record, "session_id", None),
                    "task_id": getattr(record, "task_id", None),
                }
            )
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "session_id": getattr(record, "session_id", None),
            "task_id": getattr(record, "task_id", None),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and key not in payload and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return redactor.redact(json.dumps(payload, default=str, ensure_ascii=False))


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{datetime.fromtimestamp(record.created).strftime('%H:%M:%S')} {record.levelname:<7} {record.name}: {record.getMessage()}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return redactor.redact(base)


def setup_logging(level: str, log_dir: Path, secrets: list[str]) -> None:
    redactor.add_secrets(secrets)
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    context_filter = ContextFilter()
    console = logging.StreamHandler()
    console.setFormatter(ConsoleFormatter())
    console.addFilter(context_filter)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "jarvis.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    file_handler.addFilter(context_filter)
    root.addHandler(console)
    root.addHandler(file_handler)
    for noisy in ("httpx", "httpcore", "uvicorn.access", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
