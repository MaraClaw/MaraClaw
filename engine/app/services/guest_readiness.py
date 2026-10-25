from time import monotonic

from anyio import sleep, to_thread
from python_on_whales import DockerClient
from python_on_whales.exceptions import DockerException

from app.config import get_settings
from app.services.openclaw_update_docker import _readiness_script

READY_TIMEOUT_SECONDS = 120.0
READY_INTERVAL_SECONDS = 2.0
settings = get_settings()


async def guest_ready(client: DockerClient, reference: str) -> bool:
    deadline = monotonic() + READY_TIMEOUT_SECONDS
    passes = 0
    while monotonic() < deadline:
        try:
            await to_thread.run_sync(
                lambda: client.container.execute(
                    reference, ["node", "-e", _readiness_script(settings.OPENCLAW_GATEWAY_PORT)]
                )
            )
            passes += 1
        except DockerException:
            passes = 0
        if passes == 2 and monotonic() < deadline:
            return True
        remaining = deadline - monotonic()
        if remaining > 0:
            await sleep(min(READY_INTERVAL_SECONDS, remaining))
    return False
