from time import monotonic

from anyio import sleep, to_thread
from pywhales import DockerClient

from app.services.guest_probe import probe_guest

READY_TIMEOUT_SECONDS = 120.0
READY_INTERVAL_SECONDS = 2.0


async def guest_ready(client: DockerClient, reference: str) -> bool:
    deadline = monotonic() + READY_TIMEOUT_SECONDS
    passes = 0
    while (remaining := deadline - monotonic()) > 0:
        ready = await to_thread.run_sync(probe_guest, client, reference, remaining)
        passes = passes + 1 if ready else 0
        if passes == 2 and monotonic() < deadline:
            return True
        remaining = deadline - monotonic()
        if remaining > 0:
            await sleep(min(READY_INTERVAL_SECONDS, remaining))
    return False
