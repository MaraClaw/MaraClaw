import time
from time import monotonic

from anyio import move_on_after, sleep, to_thread
from pywhales import ClientNotFoundError, DockerClient, InspectionPolicy, observation
from pywhales.client_config import ParsingError
from pywhales.exceptions import DockerException, NoSuchContainer, ObservationLimitError, ObservationTimeout

from app.core.logging import logger
from app.services.guest_probe import probe_guest

READY_TIMEOUT_SECONDS = 120.0
READY_INTERVAL_SECONDS = 2.0
OBSERVE_TIMEOUT_SECONDS = 5.0
CRASH_LOOP_RESTARTS = 2
POLICY = InspectionPolicy(max_references=1, max_argv_bytes=4096, max_response_bytes=1_048_576)


def observe_guest(
    client: DockerClient, reference: str, restarts: int | None, until: float
) -> tuple[int | None, str | None]:
    # pywhales checks deadlines against time.monotonic(); `until` is absolute so a late worker thread cannot extend it.
    try:
        with observation(POLICY, deadline=until):
            guest = client.container.inspect(reference)
            state, count = guest.state, guest.restart_count or 0
    except NoSuchContainer:
        return restarts, "removed"
    except DockerException, ParsingError, ClientNotFoundError, ObservationLimitError, ObservationTimeout, OSError:
        return restarts, None
    baseline = count if restarts is None else restarts
    if state is not None and state.status in ("exited", "dead") and state.restarting is not True:
        return baseline, "exited"
    if count - baseline > CRASH_LOOP_RESTARTS:
        return baseline, "crash_looping"
    return baseline, None


async def guest_ready(client: DockerClient, reference: str) -> bool:
    deadline = monotonic() + READY_TIMEOUT_SECONDS
    passes = 0
    restarts: int | None = None
    while (remaining := deadline - monotonic()) > 0:
        ready = await to_thread.run_sync(probe_guest, client, reference, remaining)
        passes = passes + 1 if ready else 0
        if passes == 2 and monotonic() < deadline:
            return True
        remaining = deadline - monotonic()
        if not ready and remaining > 0:
            until = time.monotonic() + min(deadline - monotonic(), OBSERVE_TIMEOUT_SECONDS)
            reason = None
            with move_on_after(remaining):
                restarts, reason = await to_thread.run_sync(
                    observe_guest, client, reference, restarts, until, abandon_on_cancel=True
                )
            if reason is not None:
                logger.warning("Guest {} {} before readiness", reference[:12], reason)
                return False
            remaining = deadline - monotonic()
        if remaining > 0:
            await sleep(min(READY_INTERVAL_SECONDS, remaining))
    return False
