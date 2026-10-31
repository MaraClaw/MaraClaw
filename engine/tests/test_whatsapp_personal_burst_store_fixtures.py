from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

import anyio
import pytest
from tests.test_chat_submission_postgres import ReceiptDatabase, receipt_db

from app.dao.channel_event_receipt_dao import channel_event_receipt_dao
from app.dao.personal_burst_dao import personal_burst_dao
from app.dao.personal_burst_input_dao import personal_burst_input_dao
from app.db.session import connection_ctx, transaction
from app.records.channel_event_receipt import ChannelEventReceiptRecord
from app.records.personal_burst import PendingUsage, PersonalBurstBatch, PersonalBurstLimits, PersonalBurstScope
from app.records.personal_burst_input import NewBurstMember, PersonalBurstInput, ReceiptKey
from app.records.whatsapp_personal import PersonalRoute
from app.services.shared_chat import shared_session_id

__all__ = ["receipt_db"]

LIMITS = PersonalBurstLimits(
    quiet_seconds=2,
    max_seconds=5,
    pending_count=50,
    pending_bytes=65536,
    batch_bytes=16384,
    notice_cooldown_seconds=30,
)


@dataclass(frozen=True, slots=True)
class BurstDatabase:
    db: ReceiptDatabase
    scope: PersonalBurstScope
    route: PersonalRoute
    limits: PersonalBurstLimits = LIMITS


@pytest.fixture
async def burst_db(receipt_db: ReceiptDatabase) -> BurstDatabase:
    db = receipt_db
    tenant, config, reply, epoch, generation = (uuid4() for _ in range(5))
    shared = shared_session_id(db.agent.id)
    route = PersonalRoute(
        config_id=config, auth_epoch=epoch, generation=generation, account_id="acct", self_jid="15550001@s.net"
    )
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'Burst QA', 'burst-qa')", {"id": tenant}
        )
        await db.first.execute(
            "INSERT INTO channel_configs (id, agent_id, channel_type, app_id, app_secret, encrypt_key, "
            "is_configured, is_connected) VALUES (%(id)s, %(agent)s, 'whatsapp', 'acct', 'x', 'y', TRUE, FALSE)",
            {"id": config, "agent": db.agent.id},
        )
        await db.first.execute(
            "INSERT INTO chat_sessions (id, agent_id, user_id, title, source_channel, is_primary) "
            "VALUES (%(id)s, %(agent)s, %(user)s, 'Shared', 'shared', TRUE)",
            {"id": shared, "agent": db.agent.id, "user": db.user.id},
        )
        await db.first.execute(
            "INSERT INTO chat_sessions (id, agent_id, user_id, title, source_channel, external_conv_id) "
            "VALUES (%(id)s, %(agent)s, %(user)s, 'Reply', 'whatsapp', %(ext)s)",
            {"id": reply, "agent": db.agent.id, "user": db.user.id, "ext": route.external_id()},
        )
    scope = PersonalBurstScope(
        agent_id=db.agent.id,
        tenant_id=tenant,
        shared_session_id=shared,
        reply_session_id=reply,
        sender_user_id=db.user.id,
        config_id=config,
        account_id="acct",
        auth_epoch=epoch,
        generation=generation,
        peer_id=route.self_jid,
    )
    return BurstDatabase(db, scope, route)


async def anchor(
    f: BurstDatabase, *, sender: UUID | None = None, content: str = "", at: datetime | None = None
) -> UUID:
    """Insert one pending shared gateway row at the guarded FIFO position (or an explicit time)."""
    created = at or await personal_burst_dao.next_created_at(f.scope.agent_id, f.scope.shared_session_id)
    gateway_id = uuid4()
    async with connection_ctx() as db:
        await db.execute(
            "INSERT INTO gateway_messages (id, agent_id, sender_user_id, conversation_id, content, status, created_at) "
            "VALUES (%(id)s, %(agent)s, %(sender)s, %(conv)s, %(content)s, 'pending', %(at)s)",
            {
                "id": gateway_id,
                "agent": f.scope.agent_id,
                "sender": sender or f.scope.sender_user_id,
                "conv": str(f.scope.shared_session_id),
                "content": content,
                "at": created,
            },
        )
    return gateway_id


async def receive(f: BurstDatabase, key: str, text: str) -> NewBurstMember:
    """Reserve the native receipt and persist the transcript row, as the admission path does."""
    receipt = ChannelEventReceiptRecord(
        config_id=f.scope.config_id,
        provider="whatsapp",
        event_key=key,
        agent_id=f.scope.agent_id,
        tenant_id=f.scope.tenant_id,
        fingerprint="v1:" + "a" * 64,
        conversation_id=f.route.external_id(),
    )
    assert await channel_event_receipt_dao.reserve(receipt)
    message_id = uuid4()
    async with connection_ctx() as db:
        await db.execute(
            "INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id) "
            "VALUES (%(id)s, %(agent)s, %(user)s, 'user', %(text)s, %(conv)s)",
            {
                "id": message_id,
                "agent": f.scope.agent_id,
                "user": f.scope.sender_user_id,
                "text": text,
                "conv": str(f.scope.shared_session_id),
            },
        )
    return NewBurstMember(ReceiptKey(f.scope.config_id, "whatsapp", key), text, message_id)


async def admit(
    f: BurstDatabase, key: str, text: str, *, limits: PersonalBurstLimits | None = None
) -> PersonalBurstBatch:
    """Append to the eligible FIFO tail or reserve a new anchor, in the caller-owned transaction."""
    limits = limits or f.limits
    async with transaction(f.db.first):
        return await admit_in_transaction(f, key, text, limits)


async def admit_in_transaction(
    f: BurstDatabase, key: str, text: str, limits: PersonalBurstLimits
) -> PersonalBurstBatch:
    member = await receive(f, key, text)
    batch = await personal_burst_dao.append_member(f.scope, member, limits)
    if batch is None:
        batch = await personal_burst_dao.create_batch(await anchor(f), f.scope, member, limits)
    return batch


async def members_of(f: BurstDatabase, gateway_id: UUID) -> list[PersonalBurstInput]:
    async with transaction(f.db.second):
        return await personal_burst_input_dao.members(gateway_id)


async def batch_of(f: BurstDatabase, gateway_id: UUID) -> PersonalBurstBatch | None:
    async with transaction(f.db.second):
        return await personal_burst_dao.get(gateway_id)


def present[T](value: T | None) -> T:
    assert value is not None
    return value


async def usage_of(f: BurstDatabase) -> PendingUsage:
    async with transaction(f.db.second):
        return await personal_burst_dao.pending_usage(f.scope.agent_id, f.scope.shared_session_id)


async def age(f: BurstDatabase, gateway_id: UUID, seconds: float) -> None:
    """Move the collection window into the past (the DB clock cannot be injected)."""
    async with transaction(f.db.first):
        await f.db.first.execute(
            "UPDATE personal_burst_batches SET first_at = first_at - make_interval(secs => %(s)s), "
            "last_at = last_at - make_interval(secs => %(s)s), ready_at = ready_at - make_interval(secs => %(s)s) "
            "WHERE gateway_id = %(id)s",
            {"s": seconds, "id": gateway_id},
        )


async def receive_committed(f: BurstDatabase, key: str) -> NewBurstMember:
    async with transaction(f.db.first):
        return await receive(f, key, "x")


async def wait_until_second_blocked(f: BurstDatabase) -> None:
    """Poll PostgreSQL until the second connection is demonstrably blocked behind the first."""
    while True:
        row = await f.db.first.fetchone(
            "SELECT pg_blocking_pids(%(pid)s) AS blockers", {"pid": f.db.second.raw.info.backend_pid}
        )
        if row is not None and row["blockers"]:
            return
        await anyio.lowlevel.checkpoint()
