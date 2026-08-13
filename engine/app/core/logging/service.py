"""Process-wide queued logging service.

Callers only format a message after the level check. I/O and exception
rendering happen on a dedicated writer thread so request/worker coroutines
are not blocked by stdout.
"""

from __future__ import annotations

import atexit
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from typing import TextIO

from app.core.logging.agentbay import disable_agentbay_logger_override
from app.core.logging.context import get_trace_id
from app.core.logging.formatters import Formatter, JsonFormatter, TextFormatter
from app.core.logging.levels import ERROR, INFO, coerce_level, level_name
from app.core.logging.record import LogRecord

_SHUTDOWN = object()
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

type Sink = Callable[[str], None]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _default_level_name() -> str:
    explicit = os.environ.get("LOG_LEVEL")
    if explicit:
        return explicit
    if os.environ.get("DEBUG", "").strip().lower() in _TRUE:
        return "DEBUG"
    return "INFO"


def _default_format_name() -> str:
    raw = os.environ.get("LOG_FORMAT", "text").strip().lower()
    return "json" if raw == "json" else "text"


def _use_color(stream: TextIO) -> bool:
    if "LOG_COLOR" in os.environ:
        return _env_bool("LOG_COLOR", True)
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _format_message(template: object, args: tuple[object, ...], kwargs: dict[str, object]) -> str:
    if isinstance(template, str):
        if not args and not kwargs:
            return template
        percent_style = "%" in template and "{" not in template
        if percent_style and args:
            try:
                return template % args
            except TypeError, ValueError:
                return f"{template} {args}"
        try:
            return template.format(*args, **kwargs)
        except IndexError, KeyError, ValueError:
            try:
                return template % args if args else template
            except TypeError, ValueError:
                return f"{template} {args} {kwargs}" if kwargs else f"{template} {args}"
    if args or kwargs:
        return f"{template} {args} {kwargs}" if kwargs else f"{template} {args}"
    return str(template)


def _build_formatter(fmt: str, *, color: bool) -> Formatter:
    if fmt == "json":
        return JsonFormatter()
    return TextFormatter(color=color)


class LoggingService:
    """Singleton-style process logger with a bounded async write queue."""

    __slots__ = (
        "_color",
        "_dropped",
        "_enqueue",
        "_formatter",
        "_min_level",
        "_queue",
        "_queue_size",
        "_sink",
        "_started",
        "_thread",
        "_write_lock",
    )

    def __init__(
        self,
        *,
        level: int | str = INFO,
        fmt: str = "text",
        enqueue: bool = True,
        queue_size: int = 8192,
        color: bool = False,
        sink: Sink | None = None,
    ) -> None:
        self._min_level = coerce_level(level)
        self._formatter = _build_formatter(fmt, color=color)
        self._enqueue = enqueue
        self._queue_size = max(1, queue_size)
        self._color = color
        self._sink = sink or sys.stdout.write
        self._queue: queue.Queue[object] = queue.Queue(maxsize=self._queue_size)
        self._thread: threading.Thread | None = None
        self._started = False
        self._dropped = 0
        self._write_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> LoggingService:
        stream = sys.stdout
        fmt = _default_format_name()
        return cls(
            level=_default_level_name(),
            fmt=fmt,
            enqueue=True,
            queue_size=_env_int("LOG_QUEUE_SIZE", 8192),
            color=_use_color(stream) and fmt != "json",
        )

    def start(self) -> None:
        if not self._enqueue or self._started:
            return
        self._thread = threading.Thread(target=self._run, name="maraclaw-log", daemon=True)
        self._thread.start()
        self._started = True

    def configure(
        self,
        *,
        level: int | str | None = None,
        fmt: str | None = None,
        enqueue: bool | None = None,
        queue_size: int | None = None,
        color: bool | None = None,
        sink: Sink | None = None,
    ) -> None:
        if level is not None:
            self._min_level = coerce_level(level)
        if sink is not None:
            self._sink = sink
        else:
            self._sink = sys.stdout.write
        if color is not None:
            self._color = color
        if fmt is not None:
            resolved = "json" if fmt == "json" else "text"
            use_color = self._color if color is not None else (self._color and resolved != "json")
            self._formatter = _build_formatter(resolved, color=use_color)
        elif color is not None:
            current = "json" if isinstance(self._formatter, JsonFormatter) else "text"
            self._formatter = _build_formatter(current, color=self._color and current != "json")
        if queue_size is not None and not self._started:
            self._queue_size = max(1, queue_size)
            self._queue = queue.Queue(maxsize=self._queue_size)
        if enqueue is None:
            return
        if enqueue and not self._started:
            self._enqueue = True
            self.start()
            return
        if not enqueue and self._started:
            self.stop()
        self._enqueue = enqueue

    def is_enabled_for(self, level: int) -> bool:
        return level >= self._min_level

    def emit(
        self,
        level: int,
        message: object,
        args: tuple[object, ...],
        kwargs: dict[str, object],
        *,
        name: str | None,
        depth: int,
        exc_info: bool | BaseException | None,
        extra: tuple[tuple[str, object], ...] | None,
        lineno: int | None = None,
    ) -> None:
        if level < self._min_level:
            return
        try:
            text = _format_message(message, args, kwargs)
            if name is not None and lineno is not None:
                caller_name, caller_line = name, lineno
            else:
                try:
                    frame = sys._getframe(depth)
                except ValueError:
                    frame = None
                if frame is None:
                    caller_name, caller_line = name or "app", 0
                else:
                    caller_name = name or str(frame.f_globals.get("__name__") or "app")
                    caller_line = int(frame.f_lineno)
            record = LogRecord(
                created=time.time(),
                levelno=level,
                levelname=level_name(level),
                message=text,
                name=caller_name,
                lineno=caller_line,
                trace_id=get_trace_id() or "-",
                exc=_coerce_exc(exc_info),
                extra=extra,
            )
        except Exception as exc:
            _write_fallback(f"logging emit failed: {type(exc).__name__}: {exc}")
            return
        if self._enqueue and self._started:
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                self._dropped += 1
            return
        self._write(record)

    def flush(self, timeout: float = 2.0) -> None:
        if not (self._enqueue and self._started):
            return
        deadline = time.monotonic() + timeout
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.001)

    def stop(self, timeout: float = 2.0) -> None:
        if not self._started:
            self._enqueue = False
            return
        try:
            self._queue.put(_SHUTDOWN, timeout=timeout)
        except queue.Full:
            self._drain_unlocked()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None
        self._started = False
        self._enqueue = False

    def _run(self) -> None:
        get = self._queue.get
        get_nowait = self._queue.get_nowait
        empty = queue.Empty
        while True:
            item = get()
            if item is _SHUTDOWN:
                self._drain_unlocked()
                return
            self._write(item)
            while True:
                try:
                    item = get_nowait()
                except empty:
                    break
                if item is _SHUTDOWN:
                    self._drain_unlocked()
                    return
                self._write(item)
            self._emit_drops()

    def _drain_unlocked(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not _SHUTDOWN:
                self._write(item)
        self._emit_drops()

    def _emit_drops(self) -> None:
        dropped = self._dropped
        if not dropped:
            return
        self._dropped = 0
        notice = LogRecord(
            created=time.time(),
            levelno=ERROR,
            levelname="ERROR",
            message=f"dropped {dropped} log records (queue full)",
            name="app.core.logging",
            lineno=0,
            trace_id="-",
            exc=None,
            extra=None,
        )
        self._write(notice)

    def _write(self, record: object) -> None:
        if not isinstance(record, LogRecord):
            return
        try:
            line = self._formatter.format(record)
            with self._write_lock:
                self._sink(line)
        except Exception as exc:
            _write_fallback(f"logging sink failed: {type(exc).__name__}: {exc}")


def _coerce_exc(exc_info: bool | BaseException | None) -> BaseException | None:
    if exc_info is None or exc_info is False:
        return None
    if exc_info is True:
        return sys.exc_info()[1]
    return exc_info


def _write_fallback(message: str) -> None:
    try:
        sys.stderr.write(message + "\n")
    except Exception:
        return


_service: LoggingService | None = None
_service_lock = threading.Lock()


def get_logging_service() -> LoggingService:
    """Return the process logger, creating it from the environment if needed."""
    global _service
    if _service is not None:
        return _service
    with _service_lock:
        if _service is None:
            _service = LoggingService.from_env()
            _service.start()
            disable_agentbay_logger_override()
            atexit.register(_atexit_shutdown)
    return _service


def _atexit_shutdown() -> None:
    if _service is not None:
        _service.stop(timeout=1.0)


def configure_logging(
    *,
    level: int | str | None = None,
    fmt: str | None = None,
    enqueue: bool | None = None,
    queue_size: int | None = None,
    color: bool | None = None,
    sink: Sink | None = None,
):
    """Configure (or reconfigure) the process logger.

    Reads ``LOG_LEVEL``, ``LOG_FORMAT``, ``LOG_QUEUE_SIZE``, ``LOG_COLOR``,
    and ``DEBUG`` from the environment when arguments are omitted. Safe to
    call more than once. Does not import ``app.config``.
    """
    service = get_logging_service()
    service.configure(
        level=level if level is not None else _default_level_name(),
        fmt=fmt if fmt is not None else _default_format_name(),
        enqueue=True if enqueue is None else enqueue,
        queue_size=queue_size if queue_size is not None else _env_int("LOG_QUEUE_SIZE", 8192),
        color=color if color is not None else (_use_color(sys.stdout) and _default_format_name() != "json"),
        sink=sink,
    )
    disable_agentbay_logger_override()
    from app.core.logging.intercept import intercept_standard_logging

    intercept_standard_logging()
    return service


def flush_logging(timeout: float = 2.0) -> None:
    """Block until queued records have been written (best effort)."""
    if _service is not None:
        _service.flush(timeout)


def shutdown_logging(timeout: float = 2.0) -> None:
    """Stop the writer thread after draining the queue."""
    global _service
    if _service is None:
        return
    _service.stop(timeout)
    _service = None
