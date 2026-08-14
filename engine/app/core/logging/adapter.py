"""Call-site logger API compatible with the loguru subset this repo uses."""

from __future__ import annotations

from app.core.logging.levels import CRITICAL, DEBUG, ERROR, INFO, SUCCESS, TRACE, WARNING, coerce_level
from app.core.logging.service import get_logging_service

_EMPTY_KWARGS: dict[str, object] = {}


class Logger:
    """Fast logger facade. Methods are no-ops when the level is disabled.

    Instances are not slotted so tests can monkeypatch ``info`` / ``exception``.
    """

    def __init__(
        self,
        name: str | None = None,
        *,
        depth: int = 0,
        exc: bool | BaseException | None = None,
        extra: tuple[tuple[str, object], ...] | None = None,
    ) -> None:
        self._name: str | None = name
        self._depth: int = depth
        self._exc: bool | BaseException | None = exc
        self._extra: tuple[tuple[str, object], ...] | None = extra

    def bind(self, **extra: object) -> Logger:
        current = dict(self._extra or ())
        current.update(extra)
        return Logger(self._name, depth=self._depth, exc=self._exc, extra=tuple(current.items()))

    def opt(
        self,
        *,
        exception: bool | BaseException | None = None,
        depth: int = 0,
        **_: object,
    ) -> Logger:
        return Logger(
            self._name,
            depth=self._depth + depth,
            exc=self._exc if exception is None else exception,
            extra=self._extra,
        )

    def trace(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit(TRACE, message, args, kwargs)

    def debug(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit(DEBUG, message, args, kwargs)

    def info(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit(INFO, message, args, kwargs)

    def success(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit(SUCCESS, message, args, kwargs)

    def warning(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit(WARNING, message, args, kwargs)

    def error(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit(ERROR, message, args, kwargs)

    def exception(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit(ERROR, message, args, kwargs, exc=True)

    def critical(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit(CRITICAL, message, args, kwargs)

    def log(self, level: int | str, message: object, *args: object, **kwargs: object) -> None:
        self._emit(coerce_level(level), message, args, kwargs)

    def complete(self) -> None:
        get_logging_service().flush()

    def _emit(
        self,
        level: int,
        message: object,
        args: tuple[object, ...],
        kwargs: dict[str, object],
        *,
        exc: bool | BaseException | None = None,
    ) -> None:
        service = get_logging_service()
        if not service.is_enabled_for(level):
            return
        service.emit(
            level,
            message,
            args,
            kwargs or _EMPTY_KWARGS,
            name=self._name,
            depth=self._depth + 3,
            exc_info=self._exc if exc is None else exc,
            extra=self._extra,
        )


_loggers: dict[str, Logger] = {}
logger = Logger()


def get_logger(name: str | None = None) -> Logger:
    """Return the process logger, optionally bound to a logger name."""
    if name is None:
        return logger
    existing = _loggers.get(name)
    if existing is None:
        existing = Logger(name)
        _loggers[name] = existing
    return existing
