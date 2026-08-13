"""Tests for the process logging service."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from typing import override

import pytest

from app.core.logging import (
    configure_logging,
    get_logger,
    intercept_standard_logging,
    logger,
    set_trace_id,
    trace_id_var,
)
from app.core.logging.levels import DEBUG, INFO
from app.core.logging.service import LoggingService


class ListSink:
    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, chunk: str) -> None:
        self.chunks.append(chunk)

    @property
    def text(self) -> str:
        return "".join(self.chunks)

    @property
    def lines(self) -> list[str]:
        return [line for line in self.text.splitlines() if line]


@pytest.fixture(autouse=True)
def _restore_process_logger() -> Iterator[None]:
    yield
    configure_logging()


def _capture(*, level: str = "DEBUG", fmt: str = "text") -> ListSink:
    sink = ListSink()
    configure_logging(level=level, fmt=fmt, enqueue=False, color=False, sink=sink.write)
    trace_id_var.set(None)
    return sink


def test_info_line_includes_message_and_module() -> None:
    sink = _capture()
    logger.info("hello-service")
    assert sink.lines
    assert "hello-service" in sink.lines[0]
    assert "test_logging_service" in sink.lines[0]
    assert "INFO" in sink.lines[0]


def test_disabled_level_skips_formatting() -> None:
    sink = _capture(level="INFO")

    class Boom:
        @override
        def __format__(self, _spec: str) -> str:
            raise AssertionError("disabled debug must not format args")

    logger.debug("hidden {}", Boom())
    assert sink.lines == []


def test_loguru_style_braces_are_formatted() -> None:
    sink = _capture()
    logger.info("agent={} status={}", "alpha", "ok")
    assert "agent=alpha status=ok" in sink.lines[0]


def test_percent_style_messages_are_interpolated() -> None:
    sink = _capture()
    logger.warning("SSO registration failed for %s provider", "feishu")
    assert "SSO registration failed for feishu provider" in sink.lines[0]


def test_missing_trace_id_is_dash_not_uuid() -> None:
    sink = _capture()
    logger.info("no-trace")
    assert "------------" in sink.lines[0]


def test_bound_trace_id_is_rendered() -> None:
    sink = _capture()
    set_trace_id("abc123def456")
    logger.info("traced")
    assert "abc123def456" in sink.lines[0]


def test_opt_exception_includes_traceback() -> None:
    sink = _capture()
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        logger.opt(exception=exc).error("task failed")
    assert "task failed" in sink.text
    assert "RuntimeError: boom" in sink.text


def test_named_logger_uses_bound_name() -> None:
    sink = _capture()
    get_logger("app.demo.widget").info("named")
    assert "app.demo.widget:" in sink.lines[0]


def test_json_format_emits_object() -> None:
    sink = _capture(fmt="json")
    set_trace_id("tracejson01")
    logger.info("json-line")
    payload = json.loads(sink.lines[0])
    assert payload["msg"] == "json-line"
    assert payload["level"] == "INFO"
    assert payload["trace_id"] == "tracejson01"
    assert payload["logger"] == "test_logging_service"


def test_stdlib_intercept_preserves_logger_name() -> None:
    sink = _capture()
    previous_handlers = logging.root.handlers[:]
    try:
        intercept_standard_logging()
        logging.getLogger("tests.stdlib.demo").error("from-stdlib")
        assert sink.lines
        assert "from-stdlib" in sink.lines[0]
        assert "tests.stdlib.demo" in sink.lines[0]
    finally:
        logging.root.handlers = previous_handlers


def test_queue_overflow_is_dropped() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_sink(_chunk: str) -> None:
        started.set()
        release.wait(timeout=1)

    service = LoggingService(
        level=INFO,
        fmt="text",
        enqueue=True,
        queue_size=1,
        color=False,
        sink=blocking_sink,
    )
    service.start()
    try:
        service.emit(INFO, "first", (), {}, name="t", depth=1, exc_info=None, extra=None, lineno=1)
        assert started.wait(timeout=1)
        service.emit(INFO, "second", (), {}, name="t", depth=1, exc_info=None, extra=None, lineno=1)
        service.emit(INFO, "third", (), {}, name="t", depth=1, exc_info=None, extra=None, lineno=1)
        assert service._dropped >= 1
    finally:
        release.set()
        service.stop()


def test_sandbox_config_import_does_not_cycle() -> None:
    from app.config import Settings
    from app.services.sandbox.config import SandboxConfig

    assert Settings is not None
    assert SandboxConfig is not None


def test_debug_constant_is_below_info() -> None:
    assert DEBUG < INFO
