"""Admit one fresh Personal WhatsApp original into the burst lane (capacity, then quota, then durable acceptance).

The caller owns the transaction and has already authorized, reserved the native receipt, bound the reply session
and locked the shared transcript session. Nothing here reads, selects or writes a guest model or file.
"""

from typing import assert_never

from app.dao.personal_burst_dao import personal_burst_dao
from app.dao.personal_burst_input_dao import personal_burst_input_dao
from app.db.session import connection_ctx
from app.records.personal_burst_input import NewBurstMember, ReceiptKey
from app.services.agent_message_quota import charge_agent_daily_message
from app.services.agent_workspace_quota import ensure_workspace_within_quota
from app.services.channels.binding import persist_channel_user_message
from app.services.channels.personal_burst_gateway import create_burst_anchor
from app.services.channels.personal_burst_limits import personal_burst_limits
from app.services.channels.personal_burst_notice import reject_personal_original
from app.services.channels.personal_burst_original import PersonalOriginal
from app.services.channels.personal_burst_outcome import (
    PersonalAccepted,
    PersonalAdmission,
    PersonalRejected,
    PersonalReplayed,
)
from app.services.quota_guard import QuotaExceededError, charge_conversation_quota


async def stored_personal_outcome(receipt: ReceiptKey) -> PersonalReplayed:
    """Report what an already-reserved native event decided; replay never changes anything."""
    stored = await personal_burst_input_dao.find_outcome(receipt)
    return PersonalReplayed(stored.outcome if stored is not None else "legacy")


async def _accept(original: PersonalOriginal) -> PersonalAccepted:
    limits = personal_burst_limits()
    scope = original.scope
    message_id = await persist_channel_user_message(
        agent_id=original.agent.id,
        user_id=original.sender.id,
        conversation_id=str(original.shared_session_id),
        content=original.text,
    )
    member = NewBurstMember(original.receipt, original.text, message_id)
    batch = await personal_burst_dao.append_member(scope, member, limits)
    if batch is not None:
        return PersonalAccepted(batch.gateway_id, batch.member_count, started_batch=False)
    anchor = await create_burst_anchor(original)
    _ = await personal_burst_dao.create_batch(anchor.id, scope, member, limits)
    return PersonalAccepted(anchor.id, 1, started_batch=True)


async def _charge_sender_and_agent(original: PersonalOriginal) -> bool:
    """Charge the sender's quota and the agent's daily cap as one unit; ``False`` when either refuses, neither charged.

    An agent over its workspace disk allowance refuses the same way. A refusal is a committed outcome here, not an
    exception, so the sender's own charge needs a savepoint to undo.
    """
    async with connection_ctx() as db:
        await db.execute("SAVEPOINT personal_message_charges")
        try:
            await charge_conversation_quota(original.sender)
            await ensure_workspace_within_quota(original.agent)
            await charge_agent_daily_message(original.agent)
        except QuotaExceededError:
            await db.execute("ROLLBACK TO SAVEPOINT personal_message_charges")
            await db.execute("RELEASE SAVEPOINT personal_message_charges")
            return False
        await db.execute("RELEASE SAVEPOINT personal_message_charges")
        return True


async def admit_personal_original(original: PersonalOriginal) -> PersonalAccepted | PersonalRejected:
    limits = personal_burst_limits()
    usage = await personal_burst_dao.pending_usage(original.agent.id, original.shared_session_id)
    verdict = usage.admission(limits, original.text_bytes)
    match verdict:
        case "oversize":
            return await reject_personal_original(original, "oversize_rejected", limits)
        case "capacity":
            return await reject_personal_original(original, "capacity_rejected", limits)
        case "admit":
            pass
        case unreachable:
            assert_never(unreachable)
    if not await _charge_sender_and_agent(original):
        return await reject_personal_original(original, "quota_rejected", limits)
    return await _accept(original)


__all__ = ["PersonalAdmission", "admit_personal_original", "stored_personal_outcome"]
