"""Broadcast acceptance and public progress projections."""

from fastapi import HTTPException

from app.core.acl_locks import lock_tenants
from app.dao.broadcast_job_dao import broadcast_job_dao
from app.db.session import connection_ctx, flush_request_transaction
from app.records.broadcast_job import BroadcastJobDraft, BroadcastJobRecord
from app.schemas.notification_responses import NotificationBroadcastJobAcceptanceResponse


def acceptance_receipt(job: BroadcastJobRecord) -> NotificationBroadcastJobAcceptanceResponse:
    return {
        "broadcast_id": job.broadcast_id,
        "status": "accepted",
        "accepted_at": job.accepted_at,
        "users_total": job.users_total,
        "agents_total": job.agents_total,
    }


def _replay(job: BroadcastJobRecord, draft: BroadcastJobDraft) -> NotificationBroadcastJobAcceptanceResponse:
    if (job.title, job.body, job.send_email) != (draft.title, draft.body, draft.send_email):
        raise HTTPException(409, "Idempotency-Key was already used for a different broadcast")
    return acceptance_receipt(job)


async def accept_broadcast_job(draft: BroadcastJobDraft) -> NotificationBroadcastJobAcceptanceResponse:
    if not draft.idempotency_key.strip():
        raise HTTPException(422, "Idempotency-Key must be nonempty")
    prior = await broadcast_job_dao.get_for_key(draft.tenant_id, draft.sender_id, draft.idempotency_key)
    if prior is not None:
        return _replay(prior, draft)
    async with connection_ctx() as db:
        await lock_tenants(db, (draft.tenant_id,))
        prior = await broadcast_job_dao.get_for_key(draft.tenant_id, draft.sender_id, draft.idempotency_key)
        if prior is not None:
            return _replay(prior, draft)
        sender = await db.fetchone(
            "SELECT id FROM users WHERE id = %(id)s AND tenant_id = %(tenant)s FOR UPDATE",
            {"id": draft.sender_id, "tenant": draft.tenant_id},
        )
        if sender is None:
            raise HTTPException(409, "Sender organization changed; retry")
        if draft.send_email:
            from app.services.system_email_service import resolve_email_config_async

            if not await resolve_email_config_async(draft.tenant_id):
                raise HTTPException(
                    400,
                    "Organization SMTP is not configured. Ask your genesis administrator to configure SMTP in Settings.",
                )
        job = await broadcast_job_dao.capture(draft)
        await flush_request_transaction()
    return acceptance_receipt(job)
