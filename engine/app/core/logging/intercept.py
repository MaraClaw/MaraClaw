"""Bridge stdlib logging into the process logger."""

from __future__ import annotations

import logging
from typing import override

from app.core.logging.levels import coerce_level
from app.core.logging.service import get_logging_service

NOISY_CONNECTION_LOGGERS = {
    # WebSocket accepted / HTTP access lines from uvicorn.
    "uvicorn.access": logging.WARNING,
    # "connection open" / "connection closed" emitted by websockets.
    "websockets": logging.WARNING,
    "websockets.server": logging.WARNING,
    "websockets.client": logging.WARNING,
    "uvicorn.protocols.websockets.websockets_impl": logging.WARNING,
    # Suppress "Failed to parse headers" warning from urllib3 when interacting with MinIO.
    "urllib3.connection": logging.ERROR,
}


class InterceptHandler(logging.Handler):
    """Forward stdlib records without walking through logging.py frames twice."""

    @override
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except TypeError, ValueError:
            message = f"{record.msg} [args={record.args}]" if record.args else str(record.msg)

        exc: BaseException | None = None
        if record.exc_info:
            exc_value = record.exc_info[1]
            if isinstance(exc_value, BaseException):
                exc = exc_value

        get_logging_service().emit(
            coerce_level(record.levelname),
            message,
            (),
            {},
            name=record.name,
            depth=1,
            exc_info=exc,
            extra=None,
            lineno=record.lineno,
        )


def quiet_noisy_connection_loggers() -> None:
    """Reduce chatty transport-level logs while keeping warnings/errors visible."""
    for logger_name, level in NOISY_CONNECTION_LOGGERS.items():
        target = logging.getLogger(logger_name)
        target.setLevel(level)


def intercept_standard_logging() -> None:
    """Redirect standard library logging into the process logger."""
    handler = InterceptHandler()
    logging.basicConfig(handlers=[handler], level=0, force=True)
    for name in logging.root.manager.loggerDict:
        std_logger = logging.getLogger(name)
        std_logger.handlers = [handler]
        std_logger.propagate = False
    quiet_noisy_connection_loggers()
