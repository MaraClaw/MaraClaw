"""Bridge stdlib logging into the process logger."""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Literal, override

from app.core.logging.levels import coerce_level
from app.core.logging.service import get_logging_service

NOISY_CONNECTION_LOGGERS = {
    "httpx": logging.WARNING,
    "httpcore": logging.INFO,
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


def _logged_exception(
    exc_info: tuple[type[BaseException] | None, BaseException | None, TracebackType | None] | Literal[False] | None,
) -> BaseException | None:
    # Logger._log normalizes truthy exc_info but forwards False unchanged.
    if exc_info is not None and exc_info is not False:
        exc_value = exc_info[1]
        if isinstance(exc_value, BaseException):
            return exc_value
    return None


class InterceptHandler(logging.Handler):
    """Forward stdlib records without walking through logging.py frames twice."""

    @override
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except TypeError, ValueError:
            message = (
                f"{record.msg} [args={record.args}]" if record.args is not None and record.args else str(record.msg)
            )

        exc = _logged_exception(record.exc_info)

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
