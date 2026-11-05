from collections.abc import Sequence
from uuid import UUID

from app.core.json_types import int_from_row, str_from_row, uuid_from_row_opt
from app.db.connection import DbConnection
from app.records.agent_storage import StorageObjectRecord, StorageUsageRecord


class AgentStorageDAO:
    """Managed S3 accounting rows. Every method runs on the caller's connection: the ledger commits on a durable
    connection of its own, while the company-transfer preflight joins the transfer transaction on purpose."""

    async def agent_tenant(self, db: DbConnection, agent_id: UUID) -> UUID | None:
        """The agent's current organization; ``None`` when the agent row is gone or unowned."""
        return uuid_from_row_opt(
            await db.fetchval("SELECT tenant_id FROM agents WHERE id = %(agent)s", {"agent": agent_id})
        )

    async def lock_usage(self, db: DbConnection, agent_id: UUID) -> int:
        """Serialize on the agent's usage row (creating it first) and return its charged bytes."""
        await db.execute(
            "INSERT INTO agent_storage_usage (agent_id) VALUES (%(agent)s) ON CONFLICT (agent_id) DO NOTHING",
            {"agent": agent_id},
        )
        return int_from_row(
            await db.fetchval(
                "SELECT charged_bytes FROM agent_storage_usage WHERE agent_id = %(agent)s FOR UPDATE",
                {"agent": agent_id},
            )
        )

    async def adjust_usage(self, db: DbConnection, agent_id: UUID, delta: int) -> None:
        if delta:
            await db.execute(
                "UPDATE agent_storage_usage SET charged_bytes = charged_bytes + %(delta)s, updated_at = now() "
                "WHERE agent_id = %(agent)s",
                {"agent": agent_id, "delta": delta},
            )

    async def usage(self, db: DbConnection, agent_id: UUID) -> StorageUsageRecord:
        row = await db.fetchone(
            "SELECT COALESCE((SELECT charged_bytes FROM agent_storage_usage WHERE agent_id = %(agent)s), 0) AS charged, "
            "COALESCE((SELECT max(GREATEST(committed_bytes, reserved_bytes)) FROM agent_storage_objects "
            "WHERE agent_id = %(agent)s), 0) AS largest",
            {"agent": agent_id},
        )
        values = row or {}
        return StorageUsageRecord(agent_id, int_from_row(values.get("charged")), int_from_row(values.get("largest")))

    async def get_object(self, db: DbConnection, agent_id: UUID, object_key: str) -> StorageObjectRecord | None:
        row = await db.fetchone(
            "SELECT * FROM agent_storage_objects WHERE agent_id = %(agent)s AND object_key = %(key)s",
            {"agent": agent_id, "key": object_key},
        )
        return StorageObjectRecord.from_row(row) if row is not None else None

    async def upsert_object(self, db: DbConnection, record: StorageObjectRecord) -> None:
        await db.execute(
            "INSERT INTO agent_storage_objects (agent_id, object_key, committed_bytes, reserved_bytes, operation_id, "
            "operation_kind, outcome_unknown, expected_etag) VALUES (%(agent)s, %(key)s, %(committed)s, %(reserved)s, "
            "%(op)s, %(kind)s, %(unknown)s, %(etag)s) ON CONFLICT (agent_id, object_key) DO UPDATE SET "
            "committed_bytes = EXCLUDED.committed_bytes, reserved_bytes = EXCLUDED.reserved_bytes, "
            "operation_id = EXCLUDED.operation_id, operation_kind = EXCLUDED.operation_kind, "
            "outcome_unknown = EXCLUDED.outcome_unknown, expected_etag = EXCLUDED.expected_etag, updated_at = now()",
            {
                "agent": record.agent_id,
                "key": record.object_key,
                "committed": record.committed_bytes,
                "reserved": record.reserved_bytes,
                "op": record.operation_id,
                "kind": record.operation_kind.value if record.operation_kind is not None else None,
                "unknown": record.outcome_unknown,
                "etag": record.expected_etag,
            },
        )

    async def drop_object(self, db: DbConnection, agent_id: UUID, object_key: str) -> None:
        await db.execute(
            "DELETE FROM agent_storage_objects WHERE agent_id = %(agent)s AND object_key = %(key)s",
            {"agent": agent_id, "key": object_key},
        )

    async def begin_delete_batch(
        self, db: DbConnection, agent_id: UUID, operation_id: UUID, object_keys: Sequence[str]
    ) -> tuple[dict[str, int], list[str]]:
        """Claim settled keys, including zero-byte tombstones for previously absent keys.

        Returns the claimed ``{key: committed_bytes}`` and the keys held back by an unresolved write.
        """
        await db.execute(
            "INSERT INTO agent_storage_objects (agent_id, object_key) "
            "SELECT %(agent)s, unnest(%(keys)s::text[]) ON CONFLICT DO NOTHING",
            {"agent": agent_id, "keys": list(object_keys)},
        )
        claimed = await db.fetchall(
            "UPDATE agent_storage_objects SET operation_id = %(op)s, operation_kind = 'delete', reserved_bytes = 0, "
            "outcome_unknown = false, expected_etag = NULL, updated_at = now() WHERE agent_id = %(agent)s "
            "AND object_key = ANY(%(keys)s) AND operation_id IS NULL "
            "RETURNING object_key, committed_bytes",
            {"agent": agent_id, "op": operation_id, "keys": list(object_keys)},
        )
        held = await db.fetchall(
            "SELECT object_key FROM agent_storage_objects WHERE agent_id = %(agent)s AND object_key = ANY(%(keys)s) "
            "AND operation_id IS NOT NULL AND operation_id <> %(op)s",
            {"agent": agent_id, "keys": list(object_keys), "op": operation_id},
        )
        return (
            {str_from_row(row["object_key"]): int_from_row(row["committed_bytes"]) for row in claimed},
            [str_from_row(row["object_key"]) for row in held],
        )

    async def pending_under(self, db: DbConnection, agent_id: UUID, prefix: str) -> int:
        """Count pending keys even when S3 has not made an in-flight write visible in its listing."""
        return int_from_row(
            await db.fetchval(
                "SELECT count(*) FROM agent_storage_objects WHERE agent_id = %(agent)s "
                "AND starts_with(object_key, %(prefix)s) AND operation_id IS NOT NULL",
                {"agent": agent_id, "prefix": prefix},
            )
        )

    async def drop_deleted_batch(
        self, db: DbConnection, agent_id: UUID, operation_id: UUID, object_keys: Sequence[str]
    ) -> int:
        """Remove rows whose deletion the provider confirmed; returns the bytes to refund."""
        rows = await db.fetchall(
            "DELETE FROM agent_storage_objects WHERE agent_id = %(agent)s AND operation_id = %(op)s "
            "AND object_key = ANY(%(keys)s) RETURNING committed_bytes",
            {"agent": agent_id, "op": operation_id, "keys": list(object_keys)},
        )
        return sum(int_from_row(row["committed_bytes"]) for row in rows)

    async def restore_batch(
        self, db: DbConnection, agent_id: UUID, operation_id: UUID, object_keys: Sequence[str]
    ) -> None:
        """Return rows to settled after the provider refused to delete them."""
        await db.execute(
            "UPDATE agent_storage_objects SET operation_id = NULL, operation_kind = NULL, outcome_unknown = false, "
            "updated_at = now() WHERE agent_id = %(agent)s AND operation_id = %(op)s AND object_key = ANY(%(keys)s)",
            {"agent": agent_id, "op": operation_id, "keys": list(object_keys)},
        )

    async def mark_unknown(self, db: DbConnection, agent_id: UUID, operation_id: UUID) -> None:
        await db.execute(
            "UPDATE agent_storage_objects SET outcome_unknown = true, updated_at = now() "
            "WHERE agent_id = %(agent)s AND operation_id = %(op)s",
            {"agent": agent_id, "op": operation_id},
        )


agent_storage_dao = AgentStorageDAO()
