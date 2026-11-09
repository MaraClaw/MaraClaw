"""One independent tenant-fenced transaction per bounded ledger slice."""

from typing import Final

from app.db.connection import DbConnection
from app.db.pool import get_pool

_EFFECTS: Final = """
WITH batch AS MATERIALIZED (
    SELECT * FROM broadcast_job_recipients
    WHERE broadcast_id = %(id)s AND ordinal > %(cursor)s ORDER BY ordinal LIMIT %(limit)s
), eligible AS MATERIALIZED (
    SELECT r.* FROM batch r JOIN users u ON r.kind = 'user' AND u.id = r.target_id
    WHERE u.tenant_id = %(tenant)s AND u.is_active IS TRUE AND %(active)s
    UNION ALL
    SELECT r.* FROM batch r JOIN agents a ON r.kind = 'agent' AND a.id = r.target_id
    WHERE a.tenant_id = %(tenant)s AND %(active)s
), notified AS (
    INSERT INTO notifications (id, user_id, agent_id, type, title, body, sender_name, is_read)
    SELECT notification_id, CASE WHEN kind = 'user' THEN target_id END,
           CASE WHEN kind = 'agent' THEN target_id END, 'broadcast', %(title)s, %(body)s, %(sender_name)s, false
    FROM eligible RETURNING user_id, agent_id
), emails AS (
    INSERT INTO broadcast_emails (id, tenant_id, broadcast_id, user_id, to_email, subject, body, message_id)
    SELECT notification_id, %(tenant)s, %(id)s, target_id, to_email, %(title)s, %(email_body)s,
           '<' || notification_id::text || '.broadcast@maraclaw.local>'
    FROM eligible WHERE kind = 'user' AND %(send_email)s AND coalesce(to_email, '') <> '' RETURNING id
)
UPDATE broadcast_jobs SET
    cursor = coalesce((SELECT max(ordinal) FROM batch), cursor),
    users_notified = users_notified + (SELECT count(*) FROM notified WHERE user_id IS NOT NULL),
    agents_notified = agents_notified + (SELECT count(*) FROM notified WHERE agent_id IS NOT NULL),
    skipped = skipped + (SELECT count(*) FROM batch) - (SELECT count(*) FROM notified),
    emails_queued = emails_queued + (SELECT count(*) FROM emails),
    status = CASE WHEN coalesce((SELECT max(ordinal) FROM batch), cursor) = users_total + agents_total
                  THEN 'completed' ELSE 'processing' END,
    completed_at = CASE WHEN coalesce((SELECT max(ordinal) FROM batch), cursor) = users_total + agents_total
                        THEN clock_timestamp() END
WHERE broadcast_id = %(id)s
"""


async def process_broadcast_batch(*, limit: int) -> bool:
    """Return after commit; interruption rolls back effects and cursor together."""
    if limit <= 0:
        return False
    async with get_pool().connection() as raw, raw.transaction():
        db = DbConnection(raw)
        tenant = await db.fetchone(
            """SELECT t.id, t.is_active FROM tenants t
               WHERE EXISTS (SELECT 1 FROM broadcast_jobs j WHERE j.tenant_id = t.id AND j.status <> 'completed')
               ORDER BY t.id LIMIT 1 FOR UPDATE OF t SKIP LOCKED"""
        )
        if tenant is None:
            return False
        job = await db.fetchone(
            """SELECT * FROM broadcast_jobs WHERE tenant_id = %(tenant)s AND status <> 'completed'
               ORDER BY accepted_at, broadcast_id LIMIT 1 FOR UPDATE""",
            {"tenant": tenant["id"]},
        )
        if job is None:
            return False
        params = {
            "id": job["broadcast_id"],
            "tenant": tenant["id"],
            "active": tenant["is_active"],
            "cursor": job["cursor"],
            "limit": limit,
        }
        # Users precede agents, and UUID order is the same as membership mutation locks.
        for table, kind in (("users", "user"), ("agents", "agent")):
            await db.execute(
                f"""SELECT target.id FROM {table} target JOIN (
                        SELECT target_id, kind FROM broadcast_job_recipients
                        WHERE broadcast_id = %(id)s AND ordinal > %(cursor)s ORDER BY ordinal LIMIT %(limit)s
                    ) r ON r.target_id = target.id AND r.kind = %(kind)s
                    WHERE target.tenant_id = %(tenant)s ORDER BY target.id FOR UPDATE OF target""",
                {**params, "kind": kind},
            )
        body, sender_name = str(job["body"]), str(job["sender_name"])
        await db.execute(
            _EFFECTS,
            {
                **params,
                "title": job["title"],
                "body": body,
                "sender_name": sender_name,
                "send_email": job["send_email"],
                "email_body": f"{body}\n\nSent by: {sender_name}" if body.strip() else f"Sent by: {sender_name}",
            },
        )
        await db.execute(
            """INSERT INTO broadcast_history
               (broadcast_id, tenant_id, sender_id, sender_name, title, body, send_email,
                users_notified, agents_notified, emails_sent)
               SELECT broadcast_id, tenant_id, sender_id, sender_name, title, body, send_email,
                      users_notified, agents_notified, emails_queued
               FROM broadcast_jobs WHERE broadcast_id = %(id)s AND status = 'completed'""",
            {"id": job["broadcast_id"]},
        )
    return True
