"""Per-agent workspace disk soft quota of tier-governed organizations (``../../docs/organization-pricing.md``).

A worker measures each agent's host workspace directory plus its guest container's writable layer. An agent whose last
measurement is over its tier's allowance refuses new human work until a later measurement is back under it; it can
still start, relay reports, run work already accepted and delete files. Measurements run off the event loop and never
inside a database connection.
"""

import asyncio
import contextvars
import os
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Final

from anyio import to_thread
from pywhales import ClientNotFoundError, DockerClient

from app.core.logging import logger
from app.core.notification_kinds import NotificationKind
from app.dao.agent_workspace_usage_dao import agent_workspace_usage_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.services.agent_manager import agent_manager
from app.services.agent_resource_pressure import record_workspace_disk_pressure
from app.services.notification_service import send_notification
from app.services.organization_plans import AmbiguousOrganizationTierError, plan_for, tier_for_tenant
from app.services.quota_guard import QuotaExceededError

LAYER_INSPECT_TIMEOUT_SECONDS: Final = 60.0
_VANISHED: Final = (FileNotFoundError, PermissionError, NotADirectoryError)
_GIB: Final = 1024**3
_DELETE_FILES: Final = "Delete files to continue."
_CLEAR_CONTAINER_DATA: Final = "Stop and start the agent to clear its container's temporary data."


def _gib(size: int) -> str:
    return f"{size / _GIB:.2f} GiB"


def _layer_is_cause(dir_bytes: int, limit_bytes: int) -> bool:
    """The host files alone fit the allowance, so the container's writable layer is what pushes usage over it."""
    return dir_bytes <= limit_bytes


def _remedy(dir_bytes: int, limit_bytes: int) -> str:
    return _CLEAR_CONTAINER_DATA if _layer_is_cause(dir_bytes, limit_bytes) else _DELETE_FILES


class WorkspaceDiskQuotaExceededError(QuotaExceededError):
    """The agent's measured workspace is over its tier allowance; ``status_code`` and ``detail`` are the HTTP contract."""

    status_code: ClassVar[int] = 507
    code: ClassVar[str] = "workspace_disk_quota_exceeded"

    def __init__(self, dir_bytes: int, layer_bytes: int, limit_bytes: int) -> None:
        used_bytes = dir_bytes + layer_bytes
        super().__init__(
            f"This agent's workspace is using {_gib(used_bytes)} of its {_gib(limit_bytes)} disk allowance. "
            f"{_remedy(dir_bytes, limit_bytes)}",
            quota_type="agent_workspace_disk",
        )
        self.dir_bytes: int = dir_bytes
        self.layer_bytes: int = layer_bytes
        self.used_bytes: int = used_bytes
        self.limit_bytes: int = limit_bytes

    @property
    def detail(self) -> dict[str, str | int]:
        return {
            "code": self.code,
            "message": self.message,
            "used_bytes": self.used_bytes,
            "limit_bytes": self.limit_bytes,
            "dir_bytes": self.dir_bytes,
            "layer_bytes": self.layer_bytes,
        }


class LayerReadUnavailableError(Exception):
    """Docker could not be asked for a layer size at all: the local CLI cannot start, or gave no answer in time."""


@dataclass(slots=True)
class LayerReads:
    """The layer-size reads of one worker pass: after the first unavailable read the pass stops asking Docker.

    A hung daemon would otherwise cost every remaining agent a full ``LAYER_INSPECT_TIMEOUT_SECONDS`` wait.
    """

    unavailable: bool = False


WorkspaceDiskQuotaExceeded = WorkspaceDiskQuotaExceededError


def measure_directory_bytes(path: Path) -> int:
    """Sum of the regular files' sizes under ``path``; a missing root is empty.

    Symlinks are neither followed nor counted, and a file reachable through several hard links counts once.
    Entries that vanish or cannot be read during the walk are skipped.
    """
    if path.is_symlink():
        return 0
    total = 0
    linked: set[tuple[int, int]] = set()
    pending = [os.fspath(path)]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except _VANISHED:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(entry.path)
                    elif stat.S_ISREG(info.st_mode):
                        if info.st_nlink > 1:
                            identity = (info.st_dev, info.st_ino)
                            if identity in linked:
                                continue
                            linked.add(identity)
                        total += info.st_size
        except _VANISHED:
            continue
    return total


def measure_layer_bytes(client: DockerClient, container_id: str, timeout: float) -> int | None:
    """Bytes in the container's writable layer; ``None`` when Docker answered without a usable size.

    Raises ``LayerReadUnavailableError`` when Docker could not be asked at all: the CLI cannot start or did not answer
    within ``timeout``. A failed exit (for example a container that no longer exists) is only ``None``: it costs no wait
    and says nothing about the other containers.

    pywhales never passes ``--size``, so this runs the local CLI itself like ``guest_probe`` does: no shell, no stdin,
    and the child is killed and reaped on timeout.
    """
    if timeout <= 0:
        return None
    try:
        command = [
            *(str(argument) for argument in client.docker_cmd),
            "container",
            "inspect",
            "--size",
            "--format",
            "{{.SizeRw}}",
            container_id,
        ]
        result = subprocess.run(  # noqa: S603 - trusted SDK prefix and a recorded container reference, without a shell.
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (ClientNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise LayerReadUnavailableError(type(exc).__name__) from exc
    if result.returncode != 0:
        return None
    reported = result.stdout.strip()
    return int(reported) if reported.isascii() and reported.isdecimal() else None


async def measure_agent(agent: AgentRecord, layers: LayerReads | None = None) -> None:
    """Measure one agent, store the result, and send the creator the single alert of a new overage.

    Agents that are not tier-governed (system agents, untenanted ones, organizations without a tier) have no allowance
    and lose any stored row. An ambiguous tier link is skipped and logged: neither measured nor forgotten. A layer size
    Docker cannot report keeps the previously stored one (zero without one), so a failed read never flaps the block.
    ``layers`` is the worker pass's shared state: once Docker is unavailable, later agents of the same pass skip the
    read and keep their stored size. Each stored measurement also feeds the resource-pressure episodes, which can never
    fail this pass.

    A measurement is ordered by when it started. If one that started later is already stored (a delete's re-measure
    overtook this one), nothing is written, fed to the episodes or alerted: the stored row decides.
    """
    layers = LayerReads() if layers is None else layers
    if agent.is_system or agent.tenant_id is None:
        _ = await agent_workspace_usage_dao.delete(record_id=agent.id)
        return
    try:
        tier = await tier_for_tenant(agent.tenant_id)
    except AmbiguousOrganizationTierError:
        logger.warning("[WorkspaceQuota] ambiguous organization tier; agent={} skipped", agent.id)
        return
    if tier is None:
        _ = await agent_workspace_usage_dao.delete(record_id=agent.id)
        return
    started = datetime.now(UTC)
    dir_bytes = await to_thread.run_sync(
        measure_directory_bytes, agent_manager._agent_dir(agent.id), abandon_on_cancel=True
    )
    layer_bytes = 0
    client = agent_manager.reacquire_docker_client()
    container_id = agent.container_id
    if container_id is not None and container_id != "":
        measured: int | None = None
        if client is not None and not layers.unavailable:
            try:
                measured = await to_thread.run_sync(
                    measure_layer_bytes, client, container_id, LAYER_INSPECT_TIMEOUT_SECONDS, abandon_on_cancel=True
                )
            except LayerReadUnavailableError as exc:
                layers.unavailable = True
                logger.warning(
                    "[WorkspaceQuota] Docker layer sizes unavailable ({}); stored sizes are kept for the rest of this "
                    "pass; first agent={}",
                    exc,
                    agent.id,
                )
        if measured is None:
            previous = await agent_workspace_usage_dao.get(agent.id)
            measured = 0 if previous is None else previous.layer_bytes
        layer_bytes = measured
    usage = await agent_workspace_usage_dao.record_measurement(
        agent.id,
        dir_bytes=dir_bytes,
        layer_bytes=layer_bytes,
        limit_bytes=plan_for(tier).workspace_disk_bytes.value,
        started=started,
    )
    if usage is None:
        stored = await agent_workspace_usage_dao.get(agent.id)
        logger.info(
            "[WorkspaceQuota] measurement superseded by a later one; agent={} stored_over={}",
            agent.id,
            stored is not None and stored.over_since is not None,
        )
        return
    await record_workspace_disk_pressure(
        agent, tier, dir_bytes=usage.dir_bytes, layer_bytes=usage.layer_bytes, limit_bytes=usage.limit_bytes
    )
    if usage.over_since is None:
        return
    async with connection_ctx():
        if await agent_workspace_usage_dao.claim_notification(agent.id) is None:
            return
        _ = await send_notification(
            user_id=agent.creator_id,
            notification_kind=NotificationKind.SYSTEM_ALERT,
            title=f"Agent {agent.name} is out of workspace disk space"[:200],
            body=(
                f"Agent {agent.name} is using {_gib(usage.used_bytes)} of its {_gib(usage.limit_bytes)} workspace "
                f"disk allowance (files {_gib(usage.dir_bytes)}, container {_gib(usage.layer_bytes)}), "
                f"so it cannot accept new messages. {_remedy(usage.dir_bytes, usage.limit_bytes)}"
            ),
            ref_id=agent.id,
            sender_name="System",
        )


async def ensure_workspace_within_quota(agent: AgentRecord) -> None:
    """Refuse new human work for an agent whose last measurement is over its allowance.

    Call it in the accepting transaction right before the daily message charge. One primary-key read; system and
    untenanted agents are exempt. A tier change takes effect at the next measurement, which uses the current limit.
    """
    if agent.is_system or agent.tenant_id is None:
        return
    usage = await agent_workspace_usage_dao.get(agent.id)
    if usage is not None and usage.over_since is not None:
        raise WorkspaceDiskQuotaExceededError(usage.dir_bytes, usage.layer_bytes, usage.limit_bytes)


async def layer_caused_block(agent: AgentRecord) -> bool:
    """True while the last measurement is over the allowance although the host files alone fit it.

    Only the guest container's writable layer is then the cause, and a new container lifts the block.
    """
    usage = await agent_workspace_usage_dao.get(agent.id)
    return usage is not None and usage.over_since is not None and _layer_is_cause(usage.dir_bytes, usage.limit_bytes)


async def remeasure_after_recreation(agent: AgentRecord) -> None:
    """Re-measure an agent whose guest container was just recreated so its block lifts at once; never raises.

    The caller is a start that may run inside a request's connection scope. The measurement runs in an empty context,
    so its directory walk and Docker inspect hold no connection or transaction of that request.
    """
    try:
        await asyncio.create_task(measure_agent(agent), context=contextvars.Context())
    except Exception as exc:
        logger.bind(exception_type=type(exc).__name__).warning(
            "[WorkspaceQuota] re-measure after recreation failed agent={}", agent.id
        )


async def refresh_workspace_usage_after_delete(agent: AgentRecord) -> None:
    """Re-measure an agent that is currently blocked so a file delete lifts the block at once; never raises."""
    try:
        usage = await agent_workspace_usage_dao.get(agent.id)
        if usage is not None and usage.over_since is not None:
            await measure_agent(agent)
    except Exception as exc:
        logger.bind(exception_type=type(exc).__name__).warning(
            "[WorkspaceQuota] re-measure after delete failed agent={}", agent.id
        )
