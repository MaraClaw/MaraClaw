"""What the Zalo child's one-second tick reads to learn that a send is due, without a row lock."""

from uuid import UUID

from app.db.session import connection_ctx
from app.records.zalo_personal import ZaloConnection
from app.services.channels.whatsapp_personal_journal import InFlight


async def has_pending(current: ZaloConnection) -> bool:
    """Lock-free peek at `ix_zalo_personal_pending`: whether `authorize_dispatch` has a candidate.

    Keep its fences identical to that call's; authorization itself stays there."""
    async with connection_ctx() as db:
        return bool(
            await db.fetchval(
                "SELECT TRUE FROM zalo_personal_sends WHERE config_id=%(config)s AND auth_epoch=%(epoch)s "
                "AND generation=%(generation)s AND account_id=%(account)s AND state='pending' LIMIT 1",
                {
                    "config": current.config_id,
                    "epoch": current.auth_epoch,
                    "generation": current.generation,
                    "account": current.account_id,
                },
            )
        )


async def settle_uncertain(current: ZaloConnection, dispatched: frozenset[UUID]) -> frozenset[UUID]:
    """Close dispatched sends the bridge left unanswered for 25 seconds as `uncertain`; return those still in flight.

    A read by primary key finds them, because no index covers `dispatch_started`; only a silent one is written."""
    async with connection_ctx() as db:
        rows = [
            InFlight.model_validate(row)
            for row in await db.fetchall(
                "SELECT command_id,dispatched_at<now()-interval '25 seconds' AS silent FROM zalo_personal_sends "
                "WHERE command_id=ANY(%(ids)s) AND state='dispatch_started'",
                {"ids": list(dispatched)},
            )
        ]
        silent = [row.command_id for row in rows if row.silent]
        if silent:
            await db.execute(
                "UPDATE zalo_personal_sends SET state='uncertain',finished_at=now() WHERE command_id=ANY(%(ids)s) "
                "AND config_id=%(config)s AND claim_token=%(claim)s AND state='dispatch_started'",
                {"ids": silent, "config": current.config_id, "claim": current.claim_token},
            )
    return frozenset(row.command_id for row in rows if not row.silent)
