from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import override

from anyio import to_thread
from python_on_whales import ClientNotFoundError, DockerClient
from python_on_whales.exceptions import DockerException

from app.config import get_settings
from app.records.agent import AgentRecord
from app.services.agent_setup_lock import setup_workspace_lock
from app.services.guest_identity import AGENT_LABEL

settings = get_settings()


@dataclass(eq=False, slots=True)
class GuestStartPreflightError(RuntimeError):
    reason: str

    @override
    def __str__(self) -> str:
        return self.reason


@dataclass(eq=False, slots=True)
class GuestAdmissionError(RuntimeError):
    code: str
    message: str

    @override
    def __str__(self) -> str:
        return self.message


def _check_capacity(client: DockerClient, agent: AgentRecord) -> None:
    guests = client.container.list(
        all=True,
        filters=[
            ("label", AGENT_LABEL),
            ("network", settings.DOCKER_NETWORK),
            ("status", "running"),
            ("status", "restarting"),
        ],
    )
    used = 0
    for guest in guests:
        if (guest.config.labels or {}).get(AGENT_LABEL) == str(agent.id):
            continue
        memory = guest.host_config.memory or 0
        if memory <= 0:
            raise GuestAdmissionError("guest_memory_unbounded", "Another running guest has no hard memory limit")
        used += memory
    if used + settings.OPENCLAW_GUEST_MEMORY_LIMIT_BYTES > settings.OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES:
        raise GuestAdmissionError("guest_memory_capacity_exceeded", "Guest aggregate memory capacity is exhausted")


@asynccontextmanager
async def guest_admission(client: DockerClient, agent: AgentRecord, agents_root: Path) -> AsyncGenerator[None]:
    if settings.OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES == 0:
        yield
        return
    # Callers hold the per-agent workspace lock first; this lock spans check, replacement and Docker run.
    async with AsyncExitStack() as stack:
        try:
            await stack.enter_async_context(setup_workspace_lock(agents_root / "_guest_admission"))
            await to_thread.run_sync(_check_capacity, client, agent)
        except (OSError, ClientNotFoundError, DockerException) as exc:
            raise GuestStartPreflightError("Could not establish guest memory capacity") from exc
        yield
