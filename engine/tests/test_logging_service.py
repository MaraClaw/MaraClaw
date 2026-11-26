"""Tests for the process logging service."""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, override
from uuid import uuid4

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Response, WebSocket
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from app.core import metrics, middleware
from app.core.logging import (
    configure_logging,
    get_logger,
    get_trace_id,
    intercept_standard_logging,
    logger,
    set_trace_id,
    trace_id_var,
)
from app.core.logging.levels import DEBUG, INFO
from app.core.logging.service import DROP_REPORT_INTERVAL, LoggingService
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


NOTICE_LOGGER = "app.core.logging"


def _emit(service: LoggingService) -> None:
    service.emit(INFO, "line", (), {}, name="t", depth=1, exc_info=None, extra=None, lineno=1)


def _dropped_in(payload: dict[str, Any]) -> int:
    """The count a drop notice reports; zero for an ordinary record."""
    return int(payload["extra"]["dropped"]) if payload["logger"] == NOTICE_LOGGER else 0


class GatedSink:
    """JSON sink whose first write parks the writer thread until ``release`` is set."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.notice_seen = threading.Event()
        self.writers: set[str] = set()
        self.payloads: list[dict[str, Any]] = []

    def write(self, chunk: str) -> None:
        payload = json.loads(chunk)
        self.writers.add(threading.current_thread().name)
        self.payloads.append(payload)
        if payload["logger"] == NOTICE_LOGGER:
            self.notice_seen.set()
        self.started.set()
        _ = self.release.wait(timeout=5)

    @property
    def reported(self) -> int:
        return sum(_dropped_in(payload) for payload in self.payloads)

    @property
    def written(self) -> int:
        return sum(1 for payload in self.payloads if payload["logger"] != NOTICE_LOGGER)


def _parked_service(sink: GatedSink, queue_size: int) -> LoggingService:
    """A started service whose writer holds one record inside the sink, so the queue only fills."""
    service = LoggingService(level=INFO, fmt="json", enqueue=True, queue_size=queue_size, color=False, sink=sink.write)
    service.start()
    _emit(service)
    assert sink.started.wait(timeout=5)
    return service


def test_concurrent_overflow_counts_every_rejected_record_exactly_once() -> None:
    # Given a writer parked in its sink and a queue that holds four records.
    sink = GatedSink()
    service = _parked_service(sink, queue_size=4)
    producers, per_producer = 8, 500
    start = threading.Barrier(producers)

    def produce() -> None:
        _ = start.wait(timeout=5)
        for _ in range(per_producer):
            _emit(service)

    threads = [threading.Thread(target=produce) for _ in range(producers)]

    # When producers race on the full queue with the interpreter switching threads as often as it can.
    switch_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    finally:
        sys.setswitchinterval(switch_interval)
    sink.release.set()
    service.stop()

    # Then no producer blocked, and each record is either written once or counted as dropped once.
    assert not any(thread.is_alive() for thread in threads)
    assert sink.reported == producers * per_producer - 4
    assert sink.written + sink.reported == 1 + producers * per_producer


def test_drop_notice_arrives_within_the_report_interval_while_the_queue_never_empties() -> None:
    # Given a sink that queues two records for every one it writes, so the queue stays full and one record drops per write.
    done = threading.Event()
    ordinary = 0
    emitted = 1
    reported = 0
    first_notice_after: int | None = None

    def sink(chunk: str) -> None:
        nonlocal ordinary, emitted, reported, first_notice_after
        payload = json.loads(chunk)
        if payload["logger"] == NOTICE_LOGGER:
            reported += _dropped_in(payload)
            if first_notice_after is None:
                first_notice_after = ordinary
            done.set()
            return
        ordinary += 1
        if done.is_set() or ordinary > 10 * DROP_REPORT_INTERVAL:
            done.set()
            return
        for _ in range(2):
            emitted += 1
            _emit(service)

    service = LoggingService(level=INFO, fmt="json", enqueue=True, queue_size=4, color=False, sink=sink)
    service.start()

    # When one record starts the cycle and the writer reports a drop notice.
    _emit(service)
    assert done.wait(timeout=10)
    service.stop()

    # Then the notice came within one report interval of written records, and the books still balance.
    assert first_notice_after is not None
    assert first_notice_after <= DROP_REPORT_INTERVAL
    assert ordinary + reported == emitted


def test_stop_with_a_full_queue_reports_pending_drops_and_never_drains_on_the_caller() -> None:
    # Given a full queue behind a parked writer, with eight records already rejected.
    sink = GatedSink()
    service = _parked_service(sink, queue_size=2)
    for _ in range(10):
        _emit(service)

    # When stop gives up waiting for the parked writer, and the writer is then released.
    service.stop(timeout=0.05)
    sink.release.set()

    # Then the writer drains the queue and reports all eight drops; the caller thread wrote nothing.
    assert sink.notice_seen.wait(timeout=5)
    assert threading.current_thread().name not in sink.writers
    assert sink.written == 3
    assert sink.reported == 8
    # And the total the metrics read still counts them although each report reset the writer's pending count.
    assert service.dropped_total == 8


def test_flush_returns_only_after_the_pending_drops_are_reported() -> None:
    # Given three rejected records behind a parked writer with two more queued.
    sink = GatedSink()
    service = _parked_service(sink, queue_size=2)
    for _ in range(5):
        _emit(service)

    # When the writer is released and the caller flushes.
    sink.release.set()
    service.flush(timeout=5)

    # Then every accepted record is written and the notice has already counted the three drops.
    assert sink.written == 3
    assert sink.reported == 3
    assert service.dropped_total == 3
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


async def _request(url: str, trace_id: str | bytes | None) -> httpx.Response:
    headers: list[tuple[bytes, bytes]] = []
    if trace_id is not None:
        headers.append((b"X-Trace-Id", trace_id.encode() if isinstance(trace_id, str) else trace_id))
    transport = httpx.ASGITransport(app=_traced_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(url, headers=headers)


async def _get(url: str) -> httpx.Response:
    return await _request(url, TRACE_ID)


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


GENERATED_TRACE_ID = re.compile(r"[0-9a-f]{12}")


def _fresh_latency_histogram(monkeypatch: pytest.MonkeyPatch) -> metrics.Histogram:
    histogram = metrics.Histogram("latency", "Latency.", ("method", "route", "status"), (1.0, 2.0))
    monkeypatch.setattr(metrics, "HTTP_REQUEST_SECONDS", histogram)
    return histogram


def _recorded_requests(histogram: metrics.Histogram) -> dict[tuple[str, str, str], float]:
    """Observation count per (method, route, status)."""
    return {
        (dict(sample.labels)["method"], dict(sample.labels)["route"], dict(sample.labels)["status"]): sample.value
        for sample in histogram.collect().samples
        if sample.name == "latency_count"
    }


async def test_each_request_logs_a_targetless_arrival_line_then_a_completion_line() -> None:
    sink = _capture(level="INFO")

    await _get(f"/api/webhooks/t/{CANARY}")

    arrival, completion = _access_text(sink).splitlines()
    assert arrival.endswith("--> GET [client: 127.0.0.1]")
    assert "<-- GET /api/webhooks/t/{token} 200 " in completion


@pytest.mark.parametrize("incoming", [TRACE_ID, str(uuid4()), "a" * 64, "Req_1.2-x"])
async def test_a_plain_incoming_trace_id_is_kept_in_the_header_and_every_access_line(incoming: str) -> None:
    sink = _capture(level="INFO")

    response = await _request("/api/webhooks/t/x", incoming)

    assert response.headers["X-Trace-Id"] == incoming
    lines = _access_text(sink).splitlines()
    assert len(lines) == 2
    assert all(incoming in line for line in lines)


@pytest.mark.parametrize("incoming", ["a" * 65, "has space", "semi;colon", "<script>", b"caf\xe9", ""])
async def test_an_unsafe_incoming_trace_id_is_replaced_by_a_generated_one(incoming: str | bytes) -> None:
    sink = _capture(level="INFO")

    response = await _request("/api/webhooks/t/x", incoming)

    generated = response.headers["X-Trace-Id"]
    assert GENERATED_TRACE_ID.fullmatch(generated)
    lines = _access_text(sink).splitlines()
    assert len(lines) == 2
    assert all(generated in line for line in lines)
    rejected = incoming.decode("latin-1") if isinstance(incoming, bytes) else incoming
    assert not rejected or not any(rejected in line for line in lines)


async def test_a_request_without_a_trace_id_gets_a_new_one_and_the_binding_ends_with_the_request() -> None:
    first = await _request("/api/webhooks/t/x", None)
    second = await _request("/api/webhooks/t/x", None)

    assert GENERATED_TRACE_ID.fullmatch(first.headers["X-Trace-Id"])
    assert first.headers["X-Trace-Id"] != second.headers["X-Trace-Id"]
    assert get_trace_id() is None


async def test_latency_runs_on_the_monotonic_clock_and_is_recorded_under_the_route_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a clock that reads 10.0 on arrival and 11.5 when the response starts.
    sink = _capture(level="INFO")
    histogram = _fresh_latency_histogram(monkeypatch)
    ticks = iter([10.0, 11.5])
    monkeypatch.setattr(middleware, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

    # When a request carrying a credential in its path and query is served.
    await _get(f"/api/webhooks/t/{CANARY}?callback={CANARY}")

    # Then the access line and the histogram both carry 1.5 s under the template, and no canary.
    assert "<-- GET /api/webhooks/t/{token} 200 1.500s" in _access_text(sink)
    samples = histogram.collect().samples
    sums = [sample for sample in samples if sample.name == "latency_sum"]
    assert [(dict(sample.labels)["route"], sample.value) for sample in sums] == [("/api/webhooks/t/{token}", 1.5)]
    assert CANARY not in repr(samples)


async def test_metrics_are_labelled_by_route_template_and_status_never_by_raw_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    histogram = _fresh_latency_histogram(monkeypatch)

    for url in (
        f"/api/webhooks/t/{CANARY}?callback={CANARY}",
        f"/api/gateway/poll/{CANARY}",
        f"/api/missing/{CANARY}?token={CANARY}",
        f"/api/boom/{CANARY}",
    ):
        await _get(url)

    assert _recorded_requests(histogram) == {
        ("GET", "/api/webhooks/t/{token}", "200"): 1,
        ("GET", "/api/gateway/poll/{key}", "200"): 1,
        ("GET", "unmatched-route", "404"): 1,
        ("GET", "/api/boom/{token}", "500"): 1,
    }
    assert CANARY not in repr(histogram.collect().samples)


async def test_websocket_and_lifespan_scopes_pass_through_untouched() -> None:
    seen: list[tuple[Scope, object]] = []

    async def inner(scope: Scope, receive: Any, send: Any) -> None:
        seen.append((scope, send))

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(_message: Message) -> None:
        return None

    traced = middleware.TraceIdMiddleware(inner)
    scopes: list[Scope] = [
        {"type": "websocket", "path": "/ws", "headers": [(b"x-trace-id", b"client-trace")]},
        {"type": "lifespan"},
    ]

    for scope in scopes:
        await traced(scope, receive, send)

    assert [(handed, sender) for handed, sender in seen] == [(scope, send) for scope in scopes]
    assert "state" not in scopes[0]
    assert get_trace_id() is None


def test_a_websocket_session_works_through_the_trace_middleware() -> None:
    app = FastAPI()
    app.add_middleware(TraceIdMiddleware)

    @app.websocket("/ws")
    async def echo(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    with TestClient(app) as client, client.websocket_connect("/ws") as session:
        session.send_text("hello")
        assert session.receive_text() == "hello"


async def test_an_error_after_the_response_started_is_counted_once_under_its_real_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an app that starts a 200 response and then fails mid-stream.
    sink = _capture(level="INFO")
    histogram = _fresh_latency_histogram(monkeypatch)
    sent: list[Message] = []

    async def inner(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError(CANARY)

    async def receive() -> Message:
        return {"type": "http.request"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/stream",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1),
    }

    # When the middleware serves it.
    with pytest.raises(RuntimeError):
        await middleware.TraceIdMiddleware(inner)(scope, receive, send)

    # Then the request is one observation under the status already sent, and the log names only the exception class.
    assert _recorded_requests(histogram) == {("GET", "unmatched-route", "200"): 1}
    assert [message["type"] for message in sent] == ["http.response.start"]
    text = _access_text(sink)
    assert "ERROR" in text
    assert "error_type=RuntimeError" in text
    assert CANARY not in text
