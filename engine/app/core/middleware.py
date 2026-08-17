"""FastAPI middleware for request tracing and logging."""

import time
import uuid
from typing import override

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.logging import logger, set_trace_id
from app.core.row_memo import clear_row_memo

_QUIET_EXACT_PATHS = frozenset({"/api/health", "/health", "/healthz"})


def is_quiet_access_log(method: str, path: str) -> bool:
    """True for high-frequency probes that should not flood INFO logs."""
    verb = method.upper()
    if path in _QUIET_EXACT_PATHS:
        return True
    if verb == "GET" and path.endswith("/unread-count"):
        return True
    if verb == "GET" and "/sessions/" in path and path.endswith("/messages"):
        return True
    if path.endswith(("/gateway/poll", "/gateway/heartbeat")):
        return verb in {"GET", "POST"}
    return False


class TraceIdMiddleware(BaseHTTPMiddleware):
    """Middleware to inject trace ID into request context and log requests."""

    @override
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Generate or extract trace ID from header
        trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())[:12]
        set_trace_id(trace_id)
        clear_row_memo()

        # Add trace ID to request state for access in endpoints
        request.state.trace_id = trace_id

        start_time = time.time()

        # Log request
        client_host = request.client.host if request.client else "-"
        path = request.url.path
        quiet = is_quiet_access_log(request.method, path)
        emit = logger.debug if quiet else logger.info
        emit(f"--> {request.method} {path} [client: {client_host}]")

        try:
            response = await call_next(request)
            duration = time.time() - start_time

            # Add trace ID to response headers
            response.headers["X-Trace-Id"] = trace_id

            # Failed probes stay visible; successes stay on debug.
            if quiet and response.status_code < 400:
                logger.debug(f"<-- {request.method} {path} {response.status_code} {duration:.3f}s")
            else:
                logger.info(f"<-- {request.method} {path} {response.status_code} {duration:.3f}s")

            return response

        except Exception as exc:
            duration = time.time() - start_time
            logger.error(f"<-- {request.method} {request.url.path} ERROR {duration:.3f}s - {exc}")
            raise
        finally:
            clear_row_memo()
