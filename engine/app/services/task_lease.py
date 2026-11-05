"""Durable, lease-based single-flight claim for agent task runs.

A run owns its task only while it holds the row's claim token. The lease is renewed in
separately committed transactions while the run is active; a run that cannot renew is
cancelled, and a lease nobody renews is returned to ``pending`` by the worker-role loop.
Recovery never reruns a task.
"""

import time
from collections.abc import Awaitable, Callable
from typing import Final
from uuid import UUID

import anyio

from app.core.logging import logger
from app.dao.task_dao import task_dao
from app.records.task import TaskClaim
from app.services.llm.background_guard import ScheduleLeaseLostError

TASK_LEASE_SECONDS: Final = 120
TASK_RENEW_SECONDS: Final = 30
LEASE_CANCEL_MARGIN_SECONDS: Final = 5
RECOVERY_INTERVAL_SECONDS: Final = 30
RECOVERY_BATCH: Final = 100
RECOVERY_LOG: Final = "⚠️ Execution lease expired; the task was returned to pending and was not rerun"
_LOCAL_WINDOW_SECONDS: Final = TASK_LEASE_SECONDS - LEASE_CANCEL_MARGIN_SECONDS
_EXPIRED: Final = "Task execution lease expired before it could be renewed"
_REVOKED: Final = "Task execution claim was revoked"


class TaskLeaseLostError(ScheduleLeaseLostError):
    """The run could not keep its lease, so another run may now own the task."""


async def claim_task(task_id: UUID, agent_id: UUID) -> TaskClaim | None:
    """Claim a run, or None when the task is missing or another run holds an unexpired lease."""
    return await task_dao.claim(task_id, agent_id, lease_seconds=TASK_LEASE_SECONDS)


class TaskLeaseGuard:
    """Fences one run on its claim: a lost or locally expired lease stops the run at its next checkpoint.

    The local deadline expires on whichever of the monotonic or wall clock passes first, so a clock
    jump on either side cannot extend a run past the lease the database would still honour.
    """

    def __init__(self, claim: TaskClaim) -> None:
        self.claim: TaskClaim = claim
        self._deadline: float = 0.0
        self._wall_deadline: float = 0.0
        self._lock: anyio.Lock = anyio.Lock()
        self._lost: TaskLeaseLostError | None = None

    def ensure_held(self) -> None:
        if self._lost is not None:
            raise self._lost
        if anyio.current_time() >= self._deadline or time.time() >= self._wall_deadline:
            raise TaskLeaseLostError(_EXPIRED)

    def remaining(self) -> float:
        """Seconds until the local deadline on the earlier of the two clocks."""
        return min(self._deadline - anyio.current_time(), self._wall_deadline - time.time())

    async def confirm(self) -> None:
        """Renew against the database, or raise ``TaskLeaseLostError`` once the claim is gone or expired."""
        self.ensure_held()
        await self.renew_within(self._deadline, self._wall_deadline)

    async def renew_within(self, bound: float, wall_bound: float) -> None:
        """Renew before either clock reaches its bound; the confirmed window counts from the renewal's start."""
        try:
            with anyio.fail_after(max(0, min(bound - anyio.current_time(), wall_bound - time.time()))):
                async with self._lock:
                    started, started_wall = anyio.current_time(), time.time()
                    if started >= bound or started_wall >= wall_bound:
                        raise TaskLeaseLostError(_EXPIRED)
                    renewed = await task_dao.renew(self.claim, lease_seconds=TASK_LEASE_SECONDS)
                    # Network/pool/commit latency counts against the lease rather than extending it.
                    if anyio.current_time() >= bound or time.time() >= wall_bound:
                        raise TaskLeaseLostError(_EXPIRED)
                    if not renewed:
                        raise TaskLeaseLostError(_REVOKED)
                    self._deadline = started + _LOCAL_WINDOW_SECONDS
                    self._wall_deadline = started_wall + _LOCAL_WINDOW_SECONDS
        except TaskLeaseLostError as lost:
            self._lost = lost
            raise
        except TimeoutError as error:
            self._lost = TaskLeaseLostError(_EXPIRED)
            raise self._lost from error
        except Exception as error:
            logger.warning("[TaskLease] Renewing task {} failed: {}", self.claim.task_id, error)
            self._lost = TaskLeaseLostError(f"Task execution lease renewal failed: {error}")
            raise self._lost from error


async def run_under_lease[Result](claim: TaskClaim, work: Callable[[TaskLeaseGuard], Awaitable[Result]]) -> Result:
    """Run ``work`` with its guard while renewing the lease; cancel it and raise ``TaskLeaseLostError`` if renewal fails.

    A renewal that raises, hangs past the point the lease could still be valid, or finds
    the token no longer owning the row counts as failed.
    """
    guard = TaskLeaseGuard(claim)
    await guard.renew_within(anyio.current_time() + _LOCAL_WINDOW_SECONDS, time.time() + _LOCAL_WINDOW_SECONDS)
    lost: TaskLeaseLostError | None = None

    async def keep_alive(scope: anyio.CancelScope) -> None:
        nonlocal lost
        while True:
            await anyio.sleep(min(TASK_RENEW_SECONDS, max(0, guard.remaining())))
            try:
                await guard.confirm()
            except TaskLeaseLostError as error:
                lost = error
                scope.cancel()
                return

    results: list[Result] = []
    failures: list[Exception] = []
    async with anyio.create_task_group() as group:
        group.start_soon(keep_alive, group.cancel_scope)
        try:
            results.append(await work(guard))
        except Exception as error:
            failures.append(error)
        finally:
            group.cancel_scope.cancel()
    if lost is not None:
        raise lost
    if failures:
        raise failures[0]
    return results[0]


async def recover_expired_leases() -> int:
    """Return every task whose lease expired to ``pending`` with a log entry; returns how many."""
    total = 0
    while True:
        recovered = await task_dao.recover_expired(limit=RECOVERY_BATCH, log=RECOVERY_LOG)
        total += len(recovered)
        if len(recovered) < RECOVERY_BATCH:
            return total


async def task_lease_recovery_loop() -> None:
    """Worker-role loop; the only place expired leases are recovered, with no startup sweep."""
    while True:
        try:
            recovered = await recover_expired_leases()
            if recovered:
                logger.warning("[TaskLease] Returned {} task(s) with expired leases to pending", recovered)
        except Exception as error:
            logger.warning("[TaskLease] Lease recovery scan failed: {}", error)
        await anyio.sleep(RECOVERY_INTERVAL_SECONDS)
