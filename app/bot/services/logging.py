from __future__ import annotations

"""Structured logging setup using structlog and orjson."""

import logging
import sys
from typing import Any

import orjson
import structlog


def _orjson_dumps(obj: Any, default: Any | None = None, **_: Any) -> str:
    """Serializer compatible with structlog.JSONRenderer.

    structlog passes optional kwargs (e.g., default) to the serializer.
    orjson supports `default` callable; ignore other kwargs.
    """
    return orjson.dumps(
        obj,
        default=default,  # type: ignore[arg-type]
        option=orjson.OPT_SORT_KEYS | orjson.OPT_APPEND_NEWLINE,
    ).decode()


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog with JSON rendering and stdlib bridge."""

    handlers = [logging.StreamHandler(sys.stdout)]
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), handlers=handlers)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(serializer=_orjson_dumps),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger() -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""

    return structlog.get_logger()
