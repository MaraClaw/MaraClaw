from collections.abc import Collection, Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from app.dao.agent_storage_dao import agent_storage_dao as dao
from app.records.agent_storage import StorageObjectRecord, StorageOperationKind
from app.services.storage_runtime.quota_errors import StorageFileTooLargeError, StorageQuotaExceededError
from app.services.storage_runtime.quota_ledger import agent_plan, ledger_tx


@dataclass(frozen=True, slots=True)
class BatchClaim:
    """Rows of one agent claimed for a multi-object delete.

    ``claimed`` maps each tracked key to its retained charge. ``held`` are keys with an unresolved write, which a
    tree delete must leave alone. Keys the ledger never tracked are simply absent from both.
    """

    agent_id: UUID
    operation_id: UUID
    claimed: dict[str, int]
    held: tuple[str, ...]


async def begin_delete_batch(agent_id: UUID, relatives: Sequence[str]) -> BatchClaim:
    operation_id = uuid4()
    async with ledger_tx() as db:
        _ = await dao.lock_usage(db, agent_id)
        claimed, held = await dao.begin_delete_batch(db, agent_id, operation_id, relatives)
    return BatchClaim(agent_id, operation_id, claimed, tuple(held))


async def finish_delete_batch(claim: BatchClaim, deleted: Collection[str], rejected: Collection[str]) -> None:
    """Refund only keys the provider confirmed deleted.

    Keys it refused return to settled with their charge intact; any other claimed key is an unknown outcome and
    stays charged.
    """
    async with ledger_tx() as db:
        _ = await dao.lock_usage(db, claim.agent_id)
        refund = await dao.drop_deleted_batch(
            db, claim.agent_id, claim.operation_id, [key for key in deleted if key in claim.claimed]
        )
        await dao.restore_batch(
            db, claim.agent_id, claim.operation_id, [key for key in rejected if key in claim.claimed]
        )
        await dao.mark_unknown(db, claim.agent_id, claim.operation_id)
        await dao.adjust_usage(db, claim.agent_id, -refund)


@dataclass(frozen=True, slots=True)
class CopyCharge:
    """One copy's destination (``relative`` to the agent), the bytes it will hold and the ETag of its source."""

    relative: str
    size: int
    etag: str


@dataclass(frozen=True, slots=True)
class CopyBatchClaim:
    """Destination rows of one agent reserved for a multi-object copy.

    ``claimed`` maps each reserved destination to the bytes charged for it. ``held`` are destinations with an unresolved
    operation, which a copy must leave alone.
    """

    agent_id: UUID
    operation_id: UUID
    claimed: dict[str, int]
    held: tuple[str, ...]


async def begin_copy_batch(agent_id: UUID, charges: Sequence[CopyCharge]) -> CopyBatchClaim:
    """Commit the conservative charge for every destination in one transaction, like ``reserve_write`` for each.

    A destination is charged ``max(current, size)`` until its copy is known. The whole batch is refused, before any
    provider call, when one file exceeds the plan's file ceiling or the total growth does not fit the allowance.
    """
    operation_id = uuid4()
    async with ledger_tx() as db:
        charged = await dao.lock_usage(db, agent_id)
        plan = await agent_plan(db, agent_id)
        rows = await dao.get_objects(db, agent_id, [charge.relative for charge in charges])
        claims: list[StorageObjectRecord] = []
        held: list[str] = []
        growth = 0
        for charge in charges:
            if plan is not None and charge.size > plan.max_file_size_bytes:
                raise StorageFileTooLargeError(int(plan.max_file_size_bytes), charge.size)
            row = rows.get(charge.relative)
            if row is not None and row.pending:
                held.append(charge.relative)
                continue
            previous = row.committed_bytes if row is not None else 0
            growth += max(previous, charge.size) - previous
            claims.append(
                StorageObjectRecord(
                    agent_id,
                    charge.relative,
                    previous,
                    charge.size,
                    operation_id,
                    StorageOperationKind.WRITE,
                    False,
                    charge.etag,
                )
            )
        if plan is not None and growth > 0 and charged + growth > plan.storage_limit_bytes:
            raise StorageQuotaExceededError(int(plan.storage_limit_bytes), charged, growth)
        await dao.upsert_objects(db, claims)
        await dao.adjust_usage(db, agent_id, growth)
    return CopyBatchClaim(
        agent_id, operation_id, {claim.object_key: claim.reserved_bytes for claim in claims}, tuple(held)
    )


async def finish_copy_batch(claim: CopyBatchClaim, landed: Collection[str], rejected: Collection[str]) -> None:
    """Commit the bytes of destinations the provider copied and return the others to what they held.

    A destination in neither list is an unknown outcome: it stays charged and blocked.
    """
    async with ledger_tx() as db:
        _ = await dao.lock_usage(db, claim.agent_id)
        delta = await dao.settle_write_batch(
            db,
            claim.agent_id,
            claim.operation_id,
            {key: claim.claimed[key] for key in landed if key in claim.claimed},
            [key for key in rejected if key in claim.claimed],
        )
        await dao.mark_unknown(db, claim.agent_id, claim.operation_id)
        await dao.adjust_usage(db, claim.agent_id, delta)
