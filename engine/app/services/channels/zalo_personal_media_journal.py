"""Private media staging precedes the irreversible, fenced SDK dispatch transition."""

from typing import Final
from uuid import UUID

from app.dao.zalo_personal_dao import fenced
from app.db.session import connection_ctx
from app.records.outbound_plan import OutboundFileChunk
from app.records.zalo_personal import ZaloConnection, ZaloMediaSend

# The bridge SDK allows 120 seconds; leave 15 seconds for its result frame.
MEDIA_DISPATCH_SECONDS: Final = 135


async def enqueue_media(destination: str, chunk: OutboundFileChunk) -> None:
    from app.services.channels.zalo_personal_journal import enqueue

    await enqueue(destination, chunk)


async def has_pending_media(current: ZaloConnection) -> bool:
    """Lock-free peek at `ix_zalo_personal_media_sends_pending`: whether `pending_media` has a descriptor.

    Keep its fences identical to that call's; authorization itself stays with the dispatch call."""
    async with connection_ctx() as db:
        return bool(
            await db.fetchval(
                "SELECT TRUE FROM zalo_personal_media_sends WHERE config_id=%(config)s AND auth_epoch=%(epoch)s "
                "AND generation=%(generation)s AND account_id=%(account)s AND state='pending' LIMIT 1",
                {
                    "config": current.config_id,
                    "epoch": current.auth_epoch,
                    "generation": current.generation,
                    "account": current.account_id,
                },
            )
        )


async def pending_media(connection: ZaloConnection) -> ZaloMediaSend | None:
    """Return the oldest pending descriptor without authorizing any provider send."""
    async with connection_ctx() as db:
        current = await fenced(connection, connected=True)
        await db.execute(
            "UPDATE zalo_personal_media_sends SET state='uncertain',finished_at=now() "
            "WHERE config_id=%(config)s AND claim_token=%(claim)s AND state='dispatch_started' "
            "AND dispatched_at < now() - %(seconds)s * interval '1 second'",
            {"config": current.config_id, "claim": current.claim_token, "seconds": MEDIA_DISPATCH_SECONDS},
        )
        raw = await db.fetchone(
            "SELECT * FROM zalo_personal_media_sends WHERE config_id=%(config)s AND auth_epoch=%(epoch)s "
            "AND generation=%(generation)s AND account_id=%(account)s AND state='pending' "
            "ORDER BY created_at,command_id LIMIT 1",
            {
                "config": current.config_id,
                "epoch": current.auth_epoch,
                "generation": current.generation,
                "account": current.account_id,
            },
        )
        return ZaloMediaSend.model_validate(raw) if raw is not None else None


async def authorize_media_dispatch(connection: ZaloConnection, command_id: UUID) -> ZaloMediaSend | None:
    """Call only after private bytes are fully staged; commit before writing the send frame."""
    from app.services.channels.sender_links import is_bound

    async with connection_ctx() as db:
        current = await fenced(connection, connected=True)
        raw = await db.fetchone(
            "SELECT * FROM zalo_personal_media_sends WHERE config_id=%(config)s AND auth_epoch=%(epoch)s "
            "AND generation=%(generation)s AND account_id=%(account)s AND state='pending' "
            "ORDER BY created_at,command_id LIMIT 1 FOR UPDATE",
            {
                "config": current.config_id,
                "epoch": current.auth_epoch,
                "generation": current.generation,
                "account": current.account_id,
            },
        )
        if raw is None:
            return None
        row = ZaloMediaSend.model_validate(raw)
        if row.command_id != command_id:
            return None
        collision = await db.fetchval(
            "SELECT TRUE FROM zalo_personal_sends WHERE command_id=%(id)s", {"id": command_id}
        )
        bound = not collision and await is_bound(row.config_id, row.account_id, row.peer_id)
        updated = await db.fetchone(
            "UPDATE zalo_personal_media_sends SET state=%(state)s,claim_token=%(claim)s,"
            "dispatched_at=CASE WHEN %(state)s='dispatch_started' THEN now() ELSE NULL END,"
            "finished_at=CASE WHEN %(state)s='cancelled' THEN now() ELSE NULL END "
            "WHERE command_id=%(id)s AND state='pending' RETURNING *",
            {"id": command_id, "state": "dispatch_started" if bound else "cancelled", "claim": current.claim_token},
        )
        return ZaloMediaSend.model_validate(updated) if bound else None


async def reject_media(connection: ZaloConnection, command_id: UUID) -> None:
    """Terminal private-source failure before authorization; transient staging failures stay pending."""
    async with connection_ctx() as db:
        current = await fenced(connection, connected=True)
        await db.execute(
            "UPDATE zalo_personal_media_sends SET state='rejected',finished_at=now() "
            "WHERE command_id=%(id)s AND config_id=%(config)s AND auth_epoch=%(epoch)s "
            "AND generation=%(generation)s AND account_id=%(account)s AND state='pending'",
            {
                "id": command_id,
                "config": current.config_id,
                "epoch": current.auth_epoch,
                "generation": current.generation,
                "account": current.account_id,
            },
        )


async def record_media_result(connection: ZaloConnection, command_id: UUID, outcome: str, message_id: str) -> None:
    from app.dao.zalo_personal_runtime_dao import observe

    async with connection_ctx() as db:
        changed = await db.fetchval(
            "UPDATE zalo_personal_media_sends SET state=%(outcome)s,message_id=%(message)s,finished_at=now() "
            "WHERE command_id=%(command)s AND config_id=%(config)s AND auth_epoch=%(epoch)s "
            "AND generation=%(generation)s AND claim_token=%(claim)s AND state='dispatch_started' RETURNING TRUE",
            {
                "outcome": outcome,
                "message": message_id or None,
                "command": command_id,
                "config": connection.config_id,
                "epoch": connection.auth_epoch,
                "generation": connection.generation,
                "claim": connection.claim_token,
            },
        )
        if changed and outcome == "accepted":
            await observe(connection, "activity")
