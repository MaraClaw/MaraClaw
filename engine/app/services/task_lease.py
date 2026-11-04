"""Durable, lease-based single-flight claim for agent task runs.

A run owns its task only while it holds the row's claim token. The lease is renewed in
separately committed transactions while the run is active; a run that cannot renew is
cancelled, and a lease nobody renews is returned to ``pending`` by the worker-role loop.
Recovery never reruns a task.
"""

from collections.abc import Awaitable, Callable
from typing import Final
from uuid import UUID

import anyio

from app.core.logging import logger
from app.dao.task_dao import task_dao
from app.records.task import TaskClaim

TASK_LEASE_SECONDS: Final = 120
TASK_RENEW_SECONDS: Final = 30
LEASE_CANCEL_MARGIN_SECONDS: Final = 5
RECOVERY_INTERVAL_SECONDS: Final = 30
RECOVERY_BATCH: Final = 100
RECOVERY_LOG: Final = "⚠️ Execution lease expired; the task was returned to pending and was not rerun"


class TaskLeaseLostError(Exception):
    """The run could not keep its lease, so another run may now own the task."""


async def claim_task(task_id: UUID, agent_id: UUID) -> TaskClaim | None:
    """Claim a run, or None when the task is missing or another run holds an unexpired lease."""
    return await task_dao.claim(task_id, agent_id, lease_seconds=TASK_LEASE_SECONDS)


async def _renew_before(claim: TaskClaim, deadline: float) -> float | None:
    started = anyio.current_time()
    try:
        with anyio.fail_after(max(0, deadline - started)):
            renewed = await task_dao.renew(claim, lease_seconds=TASK_LEASE_SECONDS)
    except Exception as error:
        logger.warning("[TaskLease] Renewing task {} failed: {}", claim.task_id, error)
        return None
    if not renewed or anyio.current_time() >= deadline:
        return None
    # Count network/pool/commit latency against the lease, rather than granting it extra lifetime.
    return started + TASK_LEASE_SECONDS - LEASE_CANCEL_MARGIN_SECONDS


async def run_under_lease[Result](claim: TaskClaim, work: Callable[[], Awaitable[Result]]) -> Result:
    """Run ``work`` while renewing the lease; cancel it and raise ``TaskLeaseLostError`` if renewal fails.

    A renewal that raises, hangs past the point the lease could still be valid, or finds
    the token no longer owning the row counts as failed.
    """
    deadline = await _renew_before(claim, anyio.current_time() + TASK_LEASE_SECONDS - LEASE_CANCEL_MARGIN_SECONDS)
    if deadline is None:
        raise TaskLeaseLostError(str(claim.task_id))
    lease_deadline = deadline
    lost = False

    async def keep_alive(scope: anyio.CancelScope) -> None:
        nonlocal lost, lease_deadline
        while True:
            await anyio.sleep(min(TASK_RENEW_SECONDS, max(0, lease_deadline - anyio.current_time())))
            renewed_deadline = await _renew_before(claim, lease_deadline)
            if renewed_deadline is None:
                lost = True
                scope.cancel()
                return
            lease_deadline = renewed_deadline

    results: list[Result] = []
    failures: list[Exception] = []
    async with anyio.create_task_group() as group:
        group.start_soon(keep_alive, group.cancel_scope)
        try:
            results.append(await work())
        except Exception as error:
            failures.append(error)
        finally:
            group.cancel_scope.cancel()
    if lost:
        raise TaskLeaseLostError(str(claim.task_id))
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
