"""Structlog setup — JSON to stdout. Loki/Promtail-friendly."""

from __future__ import annotations

import logging
import sys

import structlog


def _configure_once() -> None:
    if getattr(_configure_once, "_done", False):
        return

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )
    _configure_once._done = True  # type: ignore[attr-defined]


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    _configure_once()
    return structlog.get_logger(name)
