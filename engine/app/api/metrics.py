"""Prometheus scrape route; ``app.main`` mounts it only while ``METRICS_TOKEN`` is set and it stays out of the OpenAPI schema."""

import hmac
from collections.abc import Callable, Iterable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.core import rate_limit, redis_cache
from app.core.metrics import CONTENT_TYPE, Family, Labels, counter_family, gauge_family, render_metrics
from app.runtime.readiness import readiness_of
from app.runtime.tasks import LoopSnapshot
from app.services import cache_invalidation_worker
from app.services.channels.wecom_kf import budget_stop_count


def _loop_families(loops: Iterable[LoopSnapshot]) -> tuple[Family, ...]:
    """One series per required loop, labelled by its name alone; a loop without a progress budget has no age."""
    snapshots = tuple(loops)

    def per_loop(read: Callable[[LoopSnapshot], float | None]) -> list[tuple[Labels, float]]:
        return [((("loop", loop.name),), value) for loop in snapshots if (value := read(loop)) is not None]

    return (
        gauge_family(
            "maraclaw_worker_loop_ready",
            "1 while the required loop counts toward readiness, 0 once it is stale, ended or exhausted.",
            per_loop(lambda loop: float(loop.ready)),
        ),
        gauge_family(
            "maraclaw_worker_loop_stale",
            "1 while the required loop has not beaten within its progress budget.",
            per_loop(lambda loop: float(loop.stale)),
        ),
        gauge_family(
            "maraclaw_worker_loop_seconds_since_beat",
            "Seconds since the required loop last beat or last restarted.",
            per_loop(lambda loop: loop.seconds_since_beat),
        ),
        counter_family(
            "maraclaw_worker_loop_restarts_total",
            "Restarts the supervisor has spent on the required loop in this process.",
            per_loop(lambda loop: float(loop.restarts)),
        ),
        gauge_family(
            "maraclaw_worker_loop_exhausted",
            "1 once the required loop is down for good and liveness fails.",
            per_loop(lambda loop: float(loop.exhausted)),
        ),
    )


def build_metrics_router(token: str) -> APIRouter:
    """Router with ``GET /api/metrics``, answered only to a request whose Bearer credential equals ``token``.

    The comparison is constant time, and an empty token is refused here because it would match an empty credential.
    The handler takes no database connection and no Redis call; loop series come from in-process records.
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
        state = readiness_of(request)
        loops = _loop_families(state.tasks.loop_snapshots()) if state is not None else ()
        kf_stops = counter_family(
            "maraclaw_wecom_kf_budget_stopped",
            "WeCom customer-service callbacks stopped cleanly by the callback budget.",
            (((), budget_stop_count()),),
        )
        return PlainTextResponse(
            render_metrics(
                (
                    *loops,
                    kf_stops,
                    *redis_cache.metric_families(),
                    *rate_limit.metric_families(),
                    *cache_invalidation_worker.metric_families(),
                )
            ),
            media_type=CONTENT_TYPE,
        )

    return router
