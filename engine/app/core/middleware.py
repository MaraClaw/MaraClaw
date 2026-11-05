"""FastAPI middleware for request tracing and logging."""

import time
import uuid
from typing import override

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.routing import Route

from app.core.logging import logger, set_trace_id
from app.core.row_memo import clear_row_memo

_QUIET_EXACT_PATHS = frozenset({"/api/health", "/health", "/healthz"})
_UNMATCHED_ROUTE = "unmatched-route"


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


def route_label(request: Request) -> str:
    """Matched route template such as ``/api/agents/{id}``; never the concrete path or query.

    Read after routing has run: the router stores the matched route in the shared ASGI scope.
    """
    route = request.scope.get("route")
    if not isinstance(route, Route):
        return _UNMATCHED_ROUTE
    # FastAPI keeps include_router prefixes out of route.path; its effective route context has the full template.
    fastapi_scope: object = request.scope.get("fastapi")
    context: object = fastapi_scope.get("effective_route_context") if isinstance(fastapi_scope, dict) else None
    template: object = getattr(context, "path_format", None)
    return template if isinstance(template, str) and template else route.path


class TraceIdMiddleware(BaseHTTPMiddleware):
    """Middleware to inject trace ID into request context and log requests.

    Access lines carry method, route template, status, timing and trace id, never the request target:
    paths and queries can hold credentials (webhook tokens, callback secrets).
    """

    @override
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Generate or extract trace ID from header
        trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())[:12]
        set_trace_id(trace_id)
        clear_row_memo()

        # Add trace ID to request state for access in endpoints
        request.state.trace_id = trace_id

        start_time = time.time()

        # Log request. Routing has not run yet, so the arrival line names no target; the raw path only
        # decides the log level.
        client_host = request.client.host if request.client else "-"
        quiet = is_quiet_access_log(request.method, request.url.path)
        emit = logger.debug if quiet else logger.info
        emit(f"--> {request.method} [client: {client_host}]")

        try:
            response = await call_next(request)
            duration = time.time() - start_time

            # Add trace ID to response headers
            response.headers["X-Trace-Id"] = trace_id

            # Failed probes stay visible; successes stay on debug.
            emit_done = logger.debug if quiet and response.status_code < 400 else logger.info
            emit_done(f"<-- {request.method} {route_label(request)} {response.status_code} {duration:.3f}s")

            return response

        except Exception as exc:
            duration = time.time() - start_time
            # The exception text can echo request data, so only its class is recorded.
            logger.error(
                f"<-- {request.method} {route_label(request)} ERROR {duration:.3f}s - error_type={type(exc).__name__}"
            )
            raise
        finally:
            clear_row_memo()
