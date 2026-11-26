"""Side-effect-free readiness evaluation over runtime-owned evidence."""

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, FastAPI, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse

from app.core.events import ping_redis
from app.db.pool import ping_pool
from app.process_roles import role_enabled
from app.runtime.tasks import OwnedTasks
from app.services.realtime import realtime_router


class Readiness:
    def __init__(self, tasks: OwnedTasks) -> None:
        self.tasks: OwnedTasks = tasks
        self.roles: frozenset[str] = frozenset()
        self.initialized: bool = False
        self.draining: bool = False

    def eligible(self) -> bool:
        return (
            self.initialized
            and not self.draining
            and any(role_enabled(self.roles, role) for role in ("api", "worker", "connector"))
            and self.tasks.ready()
            and (not role_enabled(self.roles, "api") or realtime_router.ready)
        )

    def alive(self) -> bool:
        """Liveness: no required loop has used up its restart budget. Never touches the pool or Redis."""
        return not self.tasks.exhausted

    async def check(self) -> bool:
        if not self.eligible():
            return False
        database, redis = await asyncio.gather(_probe(ping_pool), _probe(ping_redis))
        # Startup/shutdown, task exits and subscription loss can race the probes.
        return database and redis and self.eligible()


async def _probe(probe: Callable[[], Awaitable[bool]]) -> bool:
    try:
        async with asyncio.timeout(2.0):
            return await probe()
    except Exception:
        # Public dependency boundary: never expose credentials or driver messages.
        return False


class ReadyResponse(BaseModel):
    status: str


class LiveResponse(BaseModel):
    status: str


router = APIRouter()


def _readiness(request: Request) -> Readiness | None:
    application = dict[str, object](request.scope).get("app")
    state: object = getattr(application.state, "readiness", None) if isinstance(application, FastAPI) else None
    return state if isinstance(state, Readiness) else None


@router.get("/api/ready", response_model=ReadyResponse)
async def ready(request: Request) -> JSONResponse:
    state = _readiness(request)
    available = state is not None and await state.check()
    return JSONResponse({"status": "ready" if available else "not_ready"}, status_code=200 if available else 503)


@router.get("/api/live", response_model=LiveResponse)
async def live(request: Request) -> JSONResponse:
    """Container liveness: 503 only once a required loop has used up its restarts; the answer itself proves the process runs."""
    state = _readiness(request)
    alive = state is None or state.alive()
    return JSONResponse({"status": "alive" if alive else "failed"}, status_code=200 if alive else 503)
