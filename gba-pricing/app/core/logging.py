"""Structured JSON logging (structlog) — one config, used everywhere."""
from __future__ import annotations

import logging
import sys

import structlog

from app.core.config import get_settings

_configured = False


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str = "gba_pricing") -> structlog.stdlib.BoundLogger:
    configure_logging()
    return structlog.get_logger(name)
