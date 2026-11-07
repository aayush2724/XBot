"""structlog configuration with secret redaction."""

from __future__ import annotations

import logging
import re
from typing import Any

import structlog
from structlog.typing import EventDict, WrappedLogger

from xbot.config import Settings

_REDACT = re.compile(r"(token|secret|password|api_key|authorization|cookie)", re.IGNORECASE)


def _redact(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict):
        if key != "event" and _REDACT.search(key):
            event_dict[key] = "[redacted]"
    return event_dict


def configure_logging(settings: Settings) -> None:
    level = logging.DEBUG if settings.env == "dev" else logging.INFO
    logging.basicConfig(level=level, format="%(message)s")
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.env == "prod"
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
