"""Logging setup shared by the Python services: JSON-lines files under logs/<service>/, every record redacted."""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from lga_common.redact import Redactor

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = None
        record.msg = self._redactor(message)
        record.args = ()
        return True


class JsonLineFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "service": self._service,
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("task_id", "step_id", "request_id", "backend", "tool"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(service: str, logs_dir: Path, level: str, redactor: Redactor, to_stderr: bool = True) -> Path:
    """Configure the root logger. Returns the log file path. stdout is never used for logs."""
    service_dir = logs_dir / service
    service_dir.mkdir(parents=True, exist_ok=True)
    log_file = service_dir / f"{time.strftime('%Y-%m-%d')}.jsonl"
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(_LEVELS.get(level.lower(), logging.INFO))
    redacting = RedactingFilter(redactor)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(JsonLineFormatter(service))
    file_handler.addFilter(redacting)
    root.addHandler(file_handler)

    if to_stderr:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(f"%(asctime)s {service} %(levelname)s %(name)s: %(message)s"))
        stream.addFilter(redacting)
        root.addHandler(stream)
    return log_file
