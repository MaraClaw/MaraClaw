"""Prometheus scrape route; ``app.main`` mounts it only while ``METRICS_TOKEN`` is set and it stays out of the OpenAPI schema."""

import hmac

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.core.metrics import CONTENT_TYPE, render_metrics


def build_metrics_router(token: str) -> APIRouter:
    """Router with ``GET /api/metrics``, answered only to a request whose Bearer credential equals ``token``.

    The comparison is constant time, and an empty token is refused here because it would match an empty credential.
    The handler takes no database connection and no Redis call.
    """
    if not token:
        raise ValueError("The metrics route needs a non-empty token")
    expected = token.encode()
    router = APIRouter()

    @router.get("/api/metrics", include_in_schema=False, response_class=PlainTextResponse)
    async def metrics(request: Request) -> PlainTextResponse:
        scheme, _, presented = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented.encode(), expected):
            raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"})
        return PlainTextResponse(render_metrics(), media_type=CONTENT_TYPE)

    return router
