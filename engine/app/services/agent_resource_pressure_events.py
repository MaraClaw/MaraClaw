"""Live OOM-kill capture for tier-governed guests: a long-lived ``docker events`` reader owned by the pressure worker.

``State.OOMKilled`` is a poor witness for a kill of the guest's main process: the ``unless-stopped`` restart clears it
within seconds, so a pass 300 seconds later almost never sees one. Polling ``docker events --since/--until`` per pass
loses events as well, because Docker keeps only about 256 in memory and exec traffic overruns them. So the worker keeps
one ``docker events --filter event=oom --filter label=<agent label>`` child alive: each output line is one kill, stored
as a ``kind='oom_kill'`` row keyed ``<container id>:<timeNano>``. The key makes the insert idempotent through the
table's partial unique index, so a replayed line or several worker processes never duplicate a row.

Lifecycle. ``read_oom_events`` runs as a child of the worker loop, so it starts with the worker and is cancelled with
it. The child is spawned shielded, and on cancellation or exit it is terminated, then killed after a grace period, and
always reaped. When the child exits or cannot start (Docker down, CLI missing) the reader restarts it with capped
backoff, which starts over once a child lived long enough to count as healthy.

Isolation. The reader never beats the loop's progress budget and never raises, so a slow, failing or absent reader
cannot make the worker unready or delay sampling. A handler runs between pipe reads and uses short DAO sessions, so no
database connection is held while waiting on the pipe, and a failing handler is logged (error type and agent id only)
without ending the stream. Malformed lines are skipped.

Correlation with the per-pass check. Docker emits ``oom`` for a kill of any process in the guest's cgroup, so this reader
also records child-process kills of a running guest. ``State.OOMKilled`` stays set while that guest keeps running and
while a crash-looping main process is ``Restarting``, so the per-pass check can still see a kill the reader already
stored, under its own key (``<container id>:<FinishedAt>``). One kill could then be two rows. The pass therefore asks
``oom_event_recorded_since`` first: a reader row for this container at or after the guest's latest ``StartedAt`` is
the kill behind the flag, and the pass inserts nothing. Without such a row (reader down, or the kill predates it) the
pass records the flag as before. The check is read-then-insert, so a pass that inspects within the milliseconds between
the kernel kill and the reader's insert can still store one kill twice, and two kills of one run, with the reader
missing one of them, count once.
"""

import subprocess
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Final
from uuid import UUID

import anyio
from anyio import BrokenResourceError, ClosedResourceError, EndOfStream, open_process, sleep, to_thread
from anyio.abc import ByteReceiveStream, Process
from pywhales import DockerClient

from app.config import get_settings
from app.core.json_types import json_loads_value, object_mapping_from
from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.agent_resource_pressure_episode_dao import agent_resource_pressure_episode_dao
from app.runtime.tasks import capped_backoff
from app.services.agent_manager import agent_manager
from app.services.agent_resource_pressure_docker import DOCKER_TIMEOUT_SECONDS, GuestInspection, inspect_guests
from app.services.guest_identity import AGENT_LABEL
from app.services.organization_plans import AmbiguousOrganizationTierError, plan_for, tier_for_tenant

EVENTS_ARGUMENTS: Final = (
    "events",
    "--filter",
    "event=oom",
    "--filter",
    f"label={AGENT_LABEL}",
    "--format",
    "{{json .}}",
)
RESTART_BASE_SECONDS: Final = 1.0
RESTART_CAP_SECONDS: Final = 60.0
# A child that ran this long was healthy: its eventual exit starts the restart backoff over.
STABLE_SECONDS: Final = 60.0
STOP_GRACE_SECONDS: Final = 5.0
READ_CHUNK_BYTES: Final = 65536
MAX_LINE_BYTES: Final = 1 << 20
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

type OomHandler = Callable[[OomEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class OomEvent:
    """One ``oom`` event of a labelled guest; ``time_nano`` is Docker's event time (1 to 3 ms after the exit)."""

    container_id: str
    agent_id: UUID
    time_nano: int

    @property
    def occurred_at(self) -> datetime:
        return _EPOCH + timedelta(microseconds=self.time_nano // 1000)

    @property
    def event_key(self) -> str:
        return f"{self.container_id}:{self.time_nano}"


def parse_oom_event(line: str) -> OomEvent | None:
    """The event in one ``docker events --format '{{json .}}'`` line; ``None`` for anything that is not a labelled oom."""
    try:
        event = object_mapping_from(json_loads_value(line))
    except ValueError:
        return None
    actor = object_mapping_from(event.get("Actor"))
    container_id = actor.get("ID")
    owner = object_mapping_from(actor.get("Attributes")).get(AGENT_LABEL)
    time_nano = event.get("timeNano")
    if event.get("Type") != "container" or event.get("Action") != "oom":
        return None
    if not isinstance(container_id, str) or container_id == "" or not isinstance(owner, str):
        return None
    if not isinstance(time_nano, int) or isinstance(time_nano, bool) or time_nano <= 0:
        return None
    try:
        agent_id = UUID(owner)
    except ValueError:
        return None
    return OomEvent(container_id, agent_id, time_nano)


async def _inspect(container_id: str) -> GuestInspection | None:
    """The guest as Docker reports it now; ``None`` when Docker cannot answer or no longer knows the container."""
    client = agent_manager.reacquire_docker_client()
    if client is None:
        return None
    found = await to_thread.run_sync(
        inspect_guests, client, [container_id], DOCKER_TIMEOUT_SECONDS, abandon_on_cancel=True
    )
    return None if found is None else found.get(container_id)


async def record_oom_event(event: OomEvent) -> None:
    """Store the kill when its agent exists, is not a system agent and belongs to a tier-governed organization.

    The row carries the guest's applied memory limit like the per-pass rows do; when Docker cannot say (it is down, or
    the guest is already gone) the tier's limit stands in, and a guest without a memory limit is not recorded.
    """
    agent = await agent_dao.get(event.agent_id)
    if agent is None or agent.is_system or agent.tenant_id is None:
        return
    try:
        tier = await tier_for_tenant(agent.tenant_id)
    except AmbiguousOrganizationTierError:
        logger.warning("[ResourcePressure] ambiguous organization tier; agent={} OOM event skipped", agent.id)
        return
    if tier is None:
        return
    tier_limit = int(plan_for(tier).guest_memory_bytes)
    inspection = await _inspect(event.container_id)
    limit = tier_limit
    detail: dict[str, object] = {"tier_limit": tier_limit}
    if inspection is not None:
        if inspection.memory_limit <= 0:
            return
        limit = inspection.memory_limit
        detail = {"exit_code": inspection.exit_code, "restart_count": inspection.restart_count, **detail}
    _ = await agent_resource_pressure_episode_dao.record_oom(
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        agent_name=agent.name,
        tier=tier,
        limit_value=limit,
        threshold_ratio=get_settings().AGENT_RESOURCE_PRESSURE_THRESHOLD,
        occurred_at=event.occurred_at,
        event_key=event.event_key,
        detail=detail,
    )


async def _handle_line(raw: bytes, handle: OomHandler) -> None:
    text = raw.decode("utf-8", errors="replace").strip()
    if text == "":
        return
    event = parse_oom_event(text)
    if event is None:
        logger.warning("[ResourcePressure] skipped a malformed docker event line ({} bytes)", len(raw))
        return
    try:
        await handle(event)
    except Exception as exc:
        logger.bind(exception_type=type(exc).__name__).warning(
            "[ResourcePressure] recording an OOM event failed agent={}", event.agent_id
        )


async def _pump(stdout: ByteReceiveStream, handle: OomHandler) -> None:
    """Feed every complete line of ``stdout`` to ``handle`` until the stream ends."""
    pending = b""
    while True:
        try:
            pending += await stdout.receive(READ_CHUNK_BYTES)
        except EndOfStream, BrokenResourceError, ClosedResourceError:
            await _handle_line(pending, handle)
            return
        while (newline := pending.find(b"\n")) >= 0:
            raw, pending = pending[:newline], pending[newline + 1 :]
            await _handle_line(raw, handle)
        if len(pending) > MAX_LINE_BYTES:
            logger.warning("[ResourcePressure] dropped an overlong docker event line ({} bytes)", len(pending))
            pending = b""


async def spawn_events_process(client: DockerClient) -> Process:
    command = [*(str(argument) for argument in client.docker_cmd), *EVENTS_ARGUMENTS]
    return await open_process(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


async def _reap(process: Process) -> None:
    """Terminate, kill after the grace period, and always wait for the child so none is left behind."""
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        with anyio.move_on_after(STOP_GRACE_SECONDS) as grace:
            _ = await process.wait()
        if grace.cancelled_caught:
            with suppress(ProcessLookupError):
                process.kill()
            _ = await process.wait()
    await process.aclose()


async def _read_once(handle: OomHandler) -> None:
    """One child's life: spawn, pump its lines until it ends, then reap it (also when cancelled)."""
    client = agent_manager.reacquire_docker_client()
    if client is None:
        logger.warning("[ResourcePressure] Docker unavailable; OOM event reader will retry")
        return
    process: Process | None = None
    exit_code: int | None = None
    try:
        # Shielded, so a cancellation cannot lose a child that was already created.
        with anyio.CancelScope(shield=True):
            process = await spawn_events_process(client)
        if process is not None and process.stdout is not None:
            await _pump(process.stdout, handle)
    finally:
        if process is not None:
            with anyio.CancelScope(shield=True):
                await _reap(process)
            exit_code = process.returncode
    logger.warning("[ResourcePressure] docker events exited code={}; restarting", exit_code)


async def read_oom_events(handle: OomHandler = record_oom_event) -> None:
    """Keep ``docker events`` running and pass each oom line to ``handle``; returns only by cancellation."""
    failures = 0
    while True:
        started = monotonic()
        try:
            await _read_once(handle)
        except Exception as exc:
            logger.bind(exception_type=type(exc).__name__).warning("[ResourcePressure] docker events reader failed")
        if monotonic() - started >= STABLE_SECONDS:
            failures = 0
        await sleep(capped_backoff(failures, base_seconds=RESTART_BASE_SECONDS, cap_seconds=RESTART_CAP_SECONDS))
        failures += 1
