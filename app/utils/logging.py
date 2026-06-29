"""Structured logging configuration for Inventory Service.

Supports two formats:
- ``json``: JSON-formatted logs (production, for log aggregators)
- ``text``: Human-readable colored logs (development)

Usage:
    from app.utils.logging import setup_logging
    setup_logging()
"""

from __future__ import annotations

import json
import logging
import logging.config
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pythonjsonlogger import jsonlogger

from app.config import settings


class CustomJsonFormatter(jsonlogger.JsonFormatter):
    """Custom JSON formatter that adds service metadata."""

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)

        if not log_record.get("timestamp"):
            log_record["timestamp"] = datetime.now(timezone.utc).isoformat()

        if not log_record.get("service"):
            log_record["service"] = "inventory-service"

        if not log_record.get("version"):
            log_record["version"] = "0.1.0"

        # Rename levelname -> level for brevity
        if log_record.get("levelname"):
            log_record["level"] = log_record.pop("levelname")


class TextFormatter(logging.Formatter):
    """Human-readable colored formatter for development."""

    COLORS = {
        "DEBUG": "\033[36m",      # Cyan
        "INFO": "\033[32m",        # Green
        "WARNING": "\033[33m",     # Yellow
        "ERROR": "\033[31m",       # Red
        "CRITICAL": "\033[41m",    # Red background
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, self.RESET)
        timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:11]
        module = record.name.split(".")[-1] if record.name else "root"

        msg = super().format(record)

        return (
            f"{color}{timestamp}{self.RESET} "
            f"[{color}{record.levelname:<8}{self.RESET}] "
            f"{module}: {msg}"
        )


def setup_logging(*, log_format: str | None = None, log_level: str | None = None) -> None:
    """Configure root logger with either JSON or text format.

    Args:
        log_format: ``"json"`` or ``"text"``. Defaults to ``settings.LOG_FORMAT``
        log_level: Override log level. Defaults to ``settings.LOG_LEVEL``
    """
    fmt = (log_format or getattr(settings, "log_format", "text")).lower()
    level = (log_level or settings.log_level).upper()

    if fmt == "json":
        formatter = CustomJsonFormatter(
            fmt="%(timestamp)s %(level)s %(name)s %(message)s",
        )
    else:
        formatter = TextFormatter("%(message)s")

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    root.handlers.clear()
    root.addHandler(handler)

    # Silence noisy libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Logging configured: format=%s level=%s", fmt, level
    )
