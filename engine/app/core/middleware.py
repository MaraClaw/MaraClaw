"""ASGI middleware for request tracing, access logging and request metrics."""

import re
import time
import uuid
from typing import Final

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.json_types import is_str_dict, object_attr
from app.core.logging import logger, trace_id_var
from app.core.metrics import observe_http_request
from app.core.row_memo import clear_row_memo

_QUIET_EXACT_PATHS = frozenset({"/api/health", "/api/live", "/api/metrics", "/health", "/healthz"})
_UNMATCHED_ROUTE = "unmatched-route"
# A client-supplied trace id is echoed in a response header and every log line, so only a short, plain one is kept.
_TRACE_ID: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")


def is_quiet_access_log(method: str, path: str) -> bool:
    """True for high-frequency probes that should not flood INFO logs."""
    verb = method.upper()
    if path in _QUIET_EXACT_PATHS:
        return True
    if verb == "GET" and path.endswith("/unread-count"):
        return True
    if verb == "GET" and "/sessions/" in path and path.endswith("/messages"):
        return True
    if path.endswith("/gateway/heartbeat"):
        return verb in {"GET", "POST"}
    return False


def route_label(scope: Scope) -> str:
    """Matched route template such as ``/api/agents/{id}``; never the concrete path or query.

    Read after routing has run: the router stores the matched route in the shared ASGI scope.
    """
    route = scope.get("route")
    if not isinstance(route, Route):
        return _UNMATCHED_ROUTE
    # FastAPI keeps include_router prefixes out of route.path; its effective route context has the full template.
    fastapi_scope: object = scope.get("fastapi")
    context: object = fastapi_scope.get("effective_route_context") if is_str_dict(fastapi_scope) else None
    template = object_attr(context, "path_format")
    return template if isinstance(template, str) and template else route.path


def _trace_id(scope: Scope) -> str:
    """The client's ``X-Trace-Id`` when it passes the strict pattern, otherwise a new 12-character id."""
    incoming = Headers(scope=scope).get("X-Trace-Id")
    if incoming is not None and _TRACE_ID.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex[:12]


class TraceIdMiddleware:
    """Pure ASGI middleware: binds the trace id, adds ``X-Trace-Id`` to the response, logs and times each HTTP request.

    Access lines carry method, route template, status, timing and trace id, never the request target:
    paths and queries can hold credentials (webhook tokens, callback secrets). The timing is seconds on the monotonic
    clock from arrival to the response start; the latency histogram records the same value under the route template.
    WebSocket and lifespan scopes pass through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app: ASGIApp = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type: object = scope.get("type")
        if scope_type != "http":
            await self.app(scope, receive, send)
            return
        trace_id = _trace_id(scope)
        token = trace_id_var.set(trace_id)
        clear_row_memo()
        request = Request(scope)
        request.state.trace_id = trace_id
        method = request.method
        quiet = is_quiet_access_log(method, request.url.path)
        emit = logger.debug if quiet else logger.info
        # Routing has not run yet, so the arrival line names no target; the raw path only decides the log level.
        emit(f"--> {method} [client: {request.client.host if request.client else '-'}]")
        start_time = time.monotonic()
        responded = False

        async def send_traced(message: Message) -> None:
            nonlocal responded
            message_type: object = message.get("type")
            status: object = message.get("status")
            if message_type == "http.response.start" and isinstance(status, int) and not responded:
                responded = True
                duration = time.monotonic() - start_time
                MutableHeaders(scope=message)["X-Trace-Id"] = trace_id
                route = route_label(scope)
                observe_http_request(method, route, status, duration)
                # Failed probes stay visible; successes stay on debug.
                emit_done = logger.debug if quiet and status < 400 else logger.info
                emit_done(f"<-- {method} {route} {status} {duration:.3f}s")
            await send(message)

        try:
            await self.app(scope, receive, send_traced)
        except Exception as exc:
            duration = time.monotonic() - start_time
            route = route_label(scope)
            if not responded:
                observe_http_request(method, route, 500, duration)
            # The exception text can echo request data, so only its class is recorded.
            logger.error(f"<-- {method} {route} ERROR {duration:.3f}s - error_type={type(exc).__name__}")
            raise
        finally:
            clear_row_memo()
            trace_id_var.reset(token)
