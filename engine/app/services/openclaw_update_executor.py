"""Worker-side OpenClaw cutover: pull, park, snapshot, start, verify, publish or roll back.

Exclusion is the per-agent workspace flock shared with setup, deletion and the
write gates; no leases or fencing. Whoever holds the lock owns the run, so a run
found in ``cutover`` under the lock was interrupted and is rolled back, never
rolled forward. A mutation is always journaled before it happens.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID

import anyio
from anyio import to_thread
from python_on_whales import ClientNotFoundError
from python_on_whales.exceptions import DockerException

from app.config import get_settings
from app.core.logging import logger
from app.records.agent import AgentRecord
from app.records.openclaw_update import OpenClawUpdateRunRecord, RunJournal
from app.services.agent_manager import guest_container_name
from app.services.agent_setup_lock import setup_workspace_lock
from app.services.guest_identity import GuestOwnershipError, require_guest_owner
from app.services.openclaw_update_docker import ContainerInfo
from app.services.openclaw_update_snapshot import commit_snapshot, drop_snapshot, restore_snapshot
from app.services.openclaw_update_store import UpgradeStore

READY_TIMEOUT_SECONDS = 120.0
READY_INTERVAL_SECONDS = 2.0
READY_PASSES = 2
ARTIFACT_LOOKBACK = timedelta(days=30)

NOT_RUNNING = "not_running"
PULL_FAILED = "pull_failed"
START_FAILED = "start_failed"
NOT_READY = "not_ready"
PUBLISH_CONFLICT = "publish_conflict"
INTERRUPTED = "interrupted"
CUTOVER_FAILED = "cutover_failed"
ROLLBACK_FAILED = "rollback_failed"


class UpgradeDockerAdapter(Protocol):
    """Blocking container operations consumed by the cutover and rollback paths."""

    def pull(self, reference: str) -> str: ...
    def inspect(self, reference: str) -> ContainerInfo | None: ...
    def set_restart(self, reference: str, policy: str) -> None: ...
    def stop(self, reference: str) -> None: ...
    def rename(self, reference: str, name: str) -> None: ...
    def remove(self, reference: str) -> None: ...
    def start(self, reference: str) -> None: ...
    def ready(self, reference: str) -> bool: ...


class UpgradeManager(Protocol):
    """Agent workspace lookup and replacement guest startup."""

    def _agent_dir(self, agent_id: UUID) -> Path: ...
    async def start_container(
        self, agent: AgentRecord, *, image: str | None = None, rehydrate: bool = True
    ) -> str | None: ...


class _CutoverError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code: str = code


@dataclass(frozen=True, slots=True)
class _Plan:
    agent_dir: Path
    old_id: str
    old_image: str
    canonical: str
    parked: str
    target: str
    old_port: int | None

    def journal(self, new_id: str | None = None) -> RunJournal:
        journal = RunJournal(
            target_image_id=self.target,
            old_container_id=self.old_id,
            old_image_id=self.old_image,
            canonical_name=self.canonical,
            parked_name=self.parked,
        )
        if self.old_port is not None:
            journal["old_port"] = self.old_port
        if new_id is not None:
            journal["new_container_id"] = new_id
        return journal


def _desired_running(agent: AgentRecord) -> bool:
    return agent.status in ("running", "idle") and not agent.is_expired and not agent.setup_pending


class UpgradeExecutor:
    def __init__(
        self,
        store: UpgradeStore,
        docker: UpgradeDockerAdapter,
        manager: UpgradeManager,
        *,
        ready_timeout: float = READY_TIMEOUT_SECONDS,
        ready_interval: float = READY_INTERVAL_SECONDS,
    ) -> None:
        self.store: UpgradeStore = store
        self.docker: UpgradeDockerAdapter = docker
        self.manager: UpgradeManager = manager
        self.ready_timeout: float = ready_timeout
        self.ready_interval: float = ready_interval
        self._artifact_cursor: UUID | None = None

    async def reconcile_success_artifacts(self) -> None:
        """Visit one bounded succeeded page; advance past skipped/clean runs to avoid starvation."""
        runs = await self.store.list_recent_succeeded(datetime.now(UTC) - ARTIFACT_LOOKBACK, self._artifact_cursor)
        for run in runs:
            self._artifact_cursor = run.id
            agent_dir = self.manager._agent_dir(run.agent_id)
            async with setup_workspace_lock(agent_dir):
                await self._cleanup_success(run, agent_dir)
        if not runs:
            self._artifact_cursor = None

    async def process(self, run_id: UUID) -> None:
        """Advance one active run. Blocking Docker/filesystem work runs in unabandoned threads."""
        run = await self.store.get_run(run_id)
        if run is None or not run.active:
            return
        target_image: str | None = None
        if run.state == "pending":
            try:
                target_image = await to_thread.run_sync(self.docker.pull, get_settings().MARACLAW_OPENCLAW_IMAGE)
            except (ClientNotFoundError, DockerException, OSError) as exc:
                logger.warning("[OpenClawUpdate] pull failed run={}: {}", run.id, exc)
                # Pending-only: a concurrent worker may already own this run in cutover.
                _ = await self.store.fail_pending(run.id, PULL_FAILED)
                return
        agent_dir = self.manager._agent_dir(run.agent_id)
        async with setup_workspace_lock(agent_dir):
            run = await self.store.get_run(run_id)
            if run is None:
                return
            if run.state == "cutover":
                await self._rollback(run, agent_dir, INTERRUPTED)
            elif run.state == "pending" and target_image is not None:
                await self._cutover(run, agent_dir, target_image)

    async def _cutover(self, run: OpenClawUpdateRunRecord, agent_dir: Path, target_image: str) -> None:
        agent = await self.store.get_agent(run.agent_id)
        if agent is None:
            return
        old = None
        if _desired_running(agent) and agent.container_id not in (None, ""):
            old = await to_thread.run_sync(self.docker.inspect, agent.container_id)
        if old is None:
            await self.store.finish_failed(run.id, NOT_RUNNING)
            return
        canonical = guest_container_name(agent.id)
        require_guest_owner(agent.id, old.id, old.agent_id)
        for reference in (canonical, f"{canonical}-prev-{str(run.id)[:8]}"):
            _ = await self._owned_container(agent.id, reference)
        plan = _Plan(
            agent_dir=agent_dir,
            old_id=old.id,
            old_image=old.image_id,
            canonical=canonical,
            parked=f"{canonical}-prev-{str(run.id)[:8]}",
            target=target_image,
            old_port=agent.container_port,
        )
        if not await self.store.begin_cutover(run.id, plan.journal()):
            return
        started = replace(run, state="cutover", journal=plan.journal())
        try:
            await self._swap(started, agent, plan)
        except _CutoverError as failure:
            await self._rollback(started, agent_dir, failure.code)
        except Exception:
            logger.exception("[OpenClawUpdate] cutover failed run={}", run.id)
            await self._rollback(started, agent_dir, CUTOVER_FAILED)
        else:
            # Cleanup failures after publication must never trigger rollback of a succeeded run.
            await self._cleanup_success(started, agent_dir)

    async def _swap(self, run: OpenClawUpdateRunRecord, agent: AgentRecord, plan: _Plan) -> None:
        await to_thread.run_sync(self.docker.set_restart, plan.old_id, "no")
        await to_thread.run_sync(self.docker.stop, plan.old_id)
        await to_thread.run_sync(self.docker.rename, plan.old_id, plan.parked)
        await self.store.requeue_inflight(agent.id)
        _ = await to_thread.run_sync(commit_snapshot, plan.agent_dir, run.id)
        replacement = replace(agent, container_id=None)
        new_id = await self.manager.start_container(replacement, image=plan.target, rehydrate=False)
        if new_id is None:
            raise _CutoverError(START_FAILED)
        await self.store.set_journal(run.id, plan.journal(new_id))
        if not await self._ready(new_id):
            raise _CutoverError(NOT_READY)
        published = await self.store.publish_success(
            run.id,
            agent.id,
            old_container_id=plan.old_id,
            new_container_id=new_id,
            port=replacement.container_port,
            image_id=plan.target,
        )
        if not published:
            raise _CutoverError(PUBLISH_CONFLICT)

    async def _owned_container(self, agent_id: UUID, reference: str) -> ContainerInfo | None:
        container = await to_thread.run_sync(self.docker.inspect, reference)
        if container is not None:
            require_guest_owner(agent_id, reference, container.agent_id)
        return container

    async def _remove_owned(self, run: OpenClawUpdateRunRecord, live_id: str | None) -> None:
        reference = run.journal.get("old_container_id")
        if reference is None or reference == live_id:
            return
        container = await self._owned_container(run.agent_id, reference)
        if container is not None and container.id != live_id:
            await to_thread.run_sync(self.docker.remove, container.id)

    async def _cleanup_success(self, candidate: OpenClawUpdateRunRecord, agent_dir: Path) -> None:
        """Caller holds the exclusive workspace lock, including the immediate publish path."""
        run = await self.store.get_run(candidate.id)
        if run is None or run.state != "succeeded":
            return
        if await self.store.active_for_agent(run.agent_id) is not None:
            return
        agent = await self.store.get_agent(run.agent_id)
        for cleanup in (
            lambda: self._remove_owned(run, agent.container_id if agent is not None else None),
            lambda: to_thread.run_sync(drop_snapshot, agent_dir, run.id),
        ):
            try:
                await cleanup()
            except (ClientNotFoundError, DockerException, GuestOwnershipError, OSError) as exc:
                logger.warning("[OpenClawUpdate] post-success cleanup left an artifact run={}: {}", run.id, exc)

    async def _ready(self, reference: str) -> bool:
        """Two consecutive in-guest ``/readyz`` passes within the bound."""
        deadline = anyio.current_time() + self.ready_timeout
        passes = 0
        while True:
            passes = passes + 1 if await to_thread.run_sync(self.docker.ready, reference) else 0
            if passes >= READY_PASSES:
                return True
            if anyio.current_time() >= deadline:
                return False
            await anyio.sleep(self.ready_interval)

    async def _rollback(self, run: OpenClawUpdateRunRecord, agent_dir: Path, code: str) -> None:
        """Remove the new writer, restore state, bring the original back. Each step is repeatable."""
        journal = run.journal
        try:
            await self._restore_original(run, agent_dir, journal)
        except Exception:
            logger.exception("[OpenClawUpdate] rollback failed run={}; artifacts preserved", run.id)
            await self.store.mark_rollback_failed(run.agent_id, journal.get("old_image_id"))
            await self.store.finish_failed(run.id, ROLLBACK_FAILED)
            return
        await self.store.finish_failed(run.id, code)

    async def _restore_original(self, run: OpenClawUpdateRunRecord, agent_dir: Path, journal: RunJournal) -> None:
        run_in = to_thread.run_sync
        old_id, canonical = journal.get("old_container_id"), journal.get("canonical_name")
        if old_id is None or canonical is None:
            return
        original = await self._owned_container(run.agent_id, old_id)
        writers: dict[str, ContainerInfo] = {}
        for writer in (journal.get("new_container_id"), canonical):
            current = await self._owned_container(run.agent_id, writer) if writer is not None else None
            if current is not None and current.id != old_id:
                writers[current.id] = current
        for writer in writers.values():
            await run_in(self.docker.remove, writer.id)
        if original is not None:
            # An interrupted run may have died before the swap stopped it; stop is a no-op otherwise.
            await run_in(self.docker.stop, old_id)
        _ = await run_in(restore_snapshot, agent_dir, run.id)
        agent = await self.store.get_agent(run.agent_id)
        if agent is None:
            if original is not None:
                await run_in(self.docker.remove, old_id)
            await run_in(drop_snapshot, agent_dir, run.id)
            return
        if original is None:
            raise _CutoverError(ROLLBACK_FAILED)
        await self.store.requeue_inflight(agent.id)
        if original.name != canonical:
            await run_in(self.docker.rename, old_id, canonical)
        await run_in(self.docker.set_restart, old_id, "unless-stopped")
        if _desired_running(agent):
            await run_in(self.docker.start, old_id)
            if not await self._ready(old_id):
                raise _CutoverError(ROLLBACK_FAILED)
        old_image = journal.get("old_image_id")
        if old_image is not None:
            await self.store.pin_image(agent.id, old_image)
        await run_in(drop_snapshot, agent_dir, run.id)


type ExecutorFactory = Callable[[], UpgradeExecutor]
