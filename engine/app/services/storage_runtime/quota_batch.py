from collections.abc import Collection, Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from app.dao.agent_storage_dao import agent_storage_dao as dao
from app.services.storage_runtime.quota_ledger import ledger_tx


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
