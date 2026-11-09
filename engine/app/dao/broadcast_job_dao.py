"""Set-based audience capture and durable broadcast lookup."""

from dataclasses import asdict
from uuid import UUID

from app.db.session import connection_ctx
from app.records.broadcast_job import BroadcastJobDraft, BroadcastJobRecord


class BroadcastJobDAO:
    async def get_for_key(self, tenant_id: UUID, sender_id: UUID, key: str) -> BroadcastJobRecord | None:
        async with connection_ctx() as db:
            row = await db.fetchone(
                """SELECT * FROM broadcast_jobs
                   WHERE tenant_id = %(tenant)s AND sender_scope_id = %(sender)s AND idempotency_key = %(key)s""",
                {"tenant": tenant_id, "sender": sender_id, "key": key},
            )
        return BroadcastJobRecord.from_row(row) if row is not None else None

    async def get(self, tenant_id: UUID, broadcast_id: UUID) -> BroadcastJobRecord | None:
        async with connection_ctx() as db:
            row = await db.fetchone(
                "SELECT * FROM broadcast_jobs WHERE tenant_id = %(tenant)s AND broadcast_id = %(id)s",
                {"tenant": tenant_id, "id": broadcast_id},
            )
        return BroadcastJobRecord.from_row(row) if row is not None else None

    async def capture(self, draft: BroadcastJobDraft) -> BroadcastJobRecord:
        """Join the tenant-locked acceptance transaction; only aggregate counts leave PostgreSQL."""
        async with connection_ctx() as db:
            [job] = await db.fetchall(
                """INSERT INTO broadcast_jobs
                   (tenant_id, sender_id, sender_scope_id, sender_name, idempotency_key, title, body, send_email)
                   VALUES (%(tenant_id)s, %(sender_id)s, %(sender_id)s, %(sender_name)s,
                           %(idempotency_key)s, %(title)s, %(body)s, %(send_email)s)
                   RETURNING broadcast_id""",
                asdict(draft),
            )
            [row] = await db.fetchall(
                """WITH audience AS (
                       SELECT 'user' AS kind, u.id AS target_id,
                              CASE WHEN %(send_email)s THEN i.email END AS to_email
                       FROM users u LEFT JOIN identities i ON i.id = u.identity_id
                       WHERE u.tenant_id = %(tenant_id)s AND u.is_active IS TRUE AND u.id <> %(sender_id)s
                       UNION ALL
                       SELECT 'agent', id, NULL FROM agents WHERE tenant_id = %(tenant_id)s
                   ), captured AS (
                       INSERT INTO broadcast_job_recipients (broadcast_id, ordinal, kind, target_id, to_email)
                       SELECT %(broadcast_id)s, row_number() OVER (ORDER BY kind, target_id),
                              kind, target_id, to_email FROM audience RETURNING kind
                   ) UPDATE broadcast_jobs SET
                       users_total = (SELECT count(*) FROM captured WHERE kind = 'user'),
                       agents_total = (SELECT count(*) FROM captured WHERE kind = 'agent')
                     WHERE broadcast_id = %(broadcast_id)s RETURNING *""",
                {**asdict(draft), "broadcast_id": job["broadcast_id"]},
            )
        return BroadcastJobRecord.from_row(row)


broadcast_job_dao = BroadcastJobDAO()
