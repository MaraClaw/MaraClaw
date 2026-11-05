"""Durable per-agent byte ledger for managed S3 objects; contract in ``docs/managed-storage-quota.md``.

Every function is one short transaction on the durable pool, serialized per agent by the usage-row lock. The tier
is read inside that lock from the agent's *current* organization, so a company transfer that holds the same lock
cannot interleave with a reservation made against the old tier.
"""

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final, assert_never
from uuid import UUID, uuid4

import anyio
import psycopg

from app.core.logging import logger
from app.dao.agent_storage_dao import agent_storage_dao as dao
from app.db.connection import DbConnection
from app.db.errors import DbError
from app.db.session import durable_transaction
from app.records.agent_storage import StorageObjectRecord, StorageOperationKind
from app.records.organization_plan import OrganizationPlan
from app.services.organization_plans import AmbiguousOrganizationTierError, plan_for, tier_for_tenant
from app.services.storage_runtime.quota_errors import (
    StorageAccountingUnavailableError,
    StorageFileTooLargeError,
    StorageQuotaExceededError,
)
from app.services.storage_runtime.quota_keys import ManagedKey
from app.services.storage_runtime.quota_outcomes import Outcome

LOCK_WAIT_MS: Final = 5_000
SETTLE_ATTEMPTS: Final = 3


@dataclass(frozen=True, slots=True)
class Reservation:
    """A committed pending row; the provider call may now be made and must be settled."""

    agent_id: UUID
    object_key: str
    operation_id: UUID
    previous_bytes: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class Blocked:
    """The key has an unresolved earlier operation and cannot be changed yet."""

    record: StorageObjectRecord


type Admission = Reservation | Blocked


@asynccontextmanager
async def shielded() -> AsyncGenerator[None]:
    """Run reserve, provider call and settle as one unit that cancellation cannot split."""
    with anyio.CancelScope(shield=True):
        yield


@asynccontextmanager
async def ledger_tx() -> AsyncGenerator[DbConnection]:
    """One durable transaction whose infrastructure failures all surface as the 503 accounting error."""
    try:
        async with durable_transaction() as db:
            await db.execute(f"SET LOCAL lock_timeout = {LOCK_WAIT_MS}")
            yield db
    except (DbError, psycopg.Error, OSError, AmbiguousOrganizationTierError) as exc:
        logger.warning(f"[storage-quota] accounting unavailable: {type(exc).__name__}")
        raise StorageAccountingUnavailableError from exc


async def settle_with_retry(attempt: Callable[[], Awaitable[None]], operation_id: UUID) -> None:
    """Run a settlement a few times; if the ledger stays unreachable the rows simply stay charged and pending."""
    for tries in range(1, SETTLE_ATTEMPTS + 1):
        try:
            await attempt()
        except StorageAccountingUnavailableError:
            if tries == SETTLE_ATTEMPTS:
                logger.error(f"[storage-quota] settlement failed, rows stay pending: op={operation_id}")
                raise
            await anyio.sleep(0.05 * tries)
        else:
            return


async def agent_plan(db: DbConnection, agent_id: UUID) -> OrganizationPlan | None:
    """The agent's current organization's plan; ``None`` for organizations without a paid tier."""
    tier = await tier_for_tenant(await dao.agent_tenant(db, agent_id))
    return plan_for(tier) if tier is not None else None


async def apply_settled(db: DbConnection, row: StorageObjectRecord, committed: int) -> None:
    """Close a pending row at ``committed`` bytes (zero removes the row) and move the usage total with it."""
    if committed == 0:
        await dao.drop_object(db, row.agent_id, row.object_key)
    else:
        await dao.upsert_object(
            db, StorageObjectRecord(row.agent_id, row.object_key, committed, 0, None, None, False, None)
        )
    await dao.adjust_usage(db, row.agent_id, committed - row.charged_bytes)


async def reserve_write(key: ManagedKey, size: int, etag: str) -> Admission:
    """Commit the conservative charge for writing ``size`` bytes: ``max(current, size)`` until the outcome is known."""
    async with ledger_tx() as db:
        charged = await dao.lock_usage(db, key.agent_id)
        plan = await agent_plan(db, key.agent_id)
        row = await dao.get_object(db, key.agent_id, key.relative)
        if plan is not None and size > plan.max_file_size_bytes:
            raise StorageFileTooLargeError(int(plan.max_file_size_bytes), size)
        if row is not None and row.pending:
            return Blocked(row)
        previous = row.committed_bytes if row is not None else 0
        growth = max(previous, size) - previous
        if plan is not None and growth > 0 and charged + growth > plan.storage_limit_bytes:
            raise StorageQuotaExceededError(int(plan.storage_limit_bytes), charged, size)
        operation_id = uuid4()
        await dao.upsert_object(
            db,
            StorageObjectRecord(
                key.agent_id, key.relative, previous, size, operation_id, StorageOperationKind.WRITE, False, etag
            ),
        )
        await dao.adjust_usage(db, key.agent_id, growth)
        return Reservation(key.agent_id, key.relative, operation_id, previous, size)


async def begin_delete(key: ManagedKey) -> Admission:
    """Mark the key's charge as retained while the provider deletes it; no row means nothing to refund."""
    async with ledger_tx() as db:
        _ = await dao.lock_usage(db, key.agent_id)
        row = await dao.get_object(db, key.agent_id, key.relative)
        if row is not None and row.pending:
            return Blocked(row)
        previous = row.committed_bytes if row is not None else 0
        operation_id = uuid4()
        await dao.upsert_object(
            db,
            StorageObjectRecord(
                key.agent_id,
                key.relative,
                previous,
                0,
                operation_id,
                StorageOperationKind.DELETE,
                False,
                None,
            ),
        )
        return Reservation(key.agent_id, key.relative, operation_id, previous, 0)


async def settle(reservation: Reservation, outcome: Outcome, adopted_bytes: int = 0) -> None:
    """Record what is known about the provider call.

    ``LANDED`` commits ``size_bytes`` (zero after a delete). ``REJECTED`` returns to the previous bytes, or to
    ``adopted_bytes`` when the provider showed a larger object that the ledger did not know. ``UNKNOWN`` keeps the
    charge and blocks the key indefinitely. HEAD/list cannot fence a request that may arrive later.
    """
    async with ledger_tx() as db:
        _ = await dao.lock_usage(db, reservation.agent_id)
        row = await dao.get_object(db, reservation.agent_id, reservation.object_key)
        if row is None or row.operation_id != reservation.operation_id:
            return
        match outcome:
            case Outcome.LANDED:
                await apply_settled(db, row, reservation.size_bytes)
            case Outcome.REJECTED:
                await apply_settled(db, row, max(row.committed_bytes, adopted_bytes))
            case Outcome.UNKNOWN:
                await dao.mark_unknown(db, reservation.agent_id, reservation.operation_id)
            case unreachable:
                assert_never(unreachable)
