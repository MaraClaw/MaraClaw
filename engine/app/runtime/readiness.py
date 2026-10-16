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


router = APIRouter()


@router.get("/api/ready", response_model=ReadyResponse)
async def ready(request: Request) -> JSONResponse:
    application = dict[str, object](request.scope).get("app")
    state: object = getattr(application.state, "readiness", None) if isinstance(application, FastAPI) else None
    available = isinstance(state, Readiness) and await state.check()
    return JSONResponse({"status": "ready" if available else "not_ready"}, status_code=200 if available else 503)
