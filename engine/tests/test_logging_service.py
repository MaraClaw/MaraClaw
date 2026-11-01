"""Tests for the process logging service."""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Iterator
from typing import override

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Response

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
from app.core.middleware import TraceIdMiddleware


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


CANARY = "canary-secret-7f3a91"
TRACE_ID = "tracecanary1"


def _traced_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TraceIdMiddleware)

    @app.get("/api/webhooks/t/{token}")
    async def receive(token: str) -> dict[str, str]:
        return {"token": token}

    @app.get("/api/boom/{token}")
    async def boom(token: str) -> None:
        raise RuntimeError(f"upstream rejected credential {token}")

    @app.get("/api/health")
    async def health(status: int = 200) -> Response:
        return Response(status_code=status)

    prefixed = APIRouter(prefix="/gateway")

    @prefixed.get("/poll/{key}")
    async def poll(key: str) -> dict[str, str]:
        return {"key": key}

    app.include_router(prefixed, prefix="/api")
    return app


def _access_text(sink: ListSink) -> str:
    return "\n".join(line for line in sink.lines if "app.core.middleware:" in line)


async def _get(url: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=_traced_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(url, headers={"X-Trace-Id": TRACE_ID})


@pytest.mark.parametrize(
    ("url", "status", "expected"),
    [
        (f"/api/webhooks/t/{CANARY}?callback={CANARY}", 200, "<-- GET /api/webhooks/t/{token} 200"),
        (f"/api/gateway/poll/{CANARY}", 200, "<-- GET /api/gateway/poll/{key} 200"),
        (f"/api/missing/{CANARY}?token={CANARY}", 404, "<-- GET unmatched-route 404"),
        (f"/api/boom/{CANARY}", 500, "<-- GET /api/boom/{token} ERROR"),
    ],
)
async def test_access_lines_name_the_route_template_never_the_request_target(
    url: str, status: int, expected: str
) -> None:
    # Given: credential canaries in the path and the query string
    sink = _capture(level="INFO")

    # When: the request is matched, unmatched, or raises from its handler
    response = await _get(url)

    # Then: method, status, timing and trace id survive while no canary or exception text is logged
    access = _access_text(sink)
    assert response.status_code == status
    assert expected in access
    assert re.search(r"\d+\.\d{3}s", access)
    assert all(TRACE_ID in line for line in access.splitlines())
    assert CANARY not in access
    assert ("error_type=RuntimeError" in access) is (status == 500)


async def test_quiet_probes_stay_off_info_until_they_fail() -> None:
    sink = _capture(level="INFO")

    healthy = await _get("/api/health")
    assert healthy.status_code == 200
    assert _access_text(sink) == ""

    down = await _get("/api/health?status=503")
    assert down.status_code == 503
    assert "<-- GET /api/health 503" in _access_text(sink)
