from datetime import UTC, datetime

from tests.channel_submission_fixtures import ChannelDatabase, channel_db, receipt_db

from app.dao.zalo_personal_auth_dao import write_auth
from app.dao.zalo_personal_dao import claim, get_personal
from app.records.zalo_personal import ZaloConnection, ZaloRoute
from app.services.channels.zalo_personal_control import start
from app.services.channels.zalo_personal_events import handle_frame
from app.services.channels.zalo_personal_protocol import Identity, Message, Ready

__all__ = ["channel_db", "receipt_db"]


async def claimed(f: ChannelDatabase) -> ZaloConnection:
    await start(f.db.agent.id, f.db.user.id, None)
    current = await get_personal(f.db.agent.id)
    assert current is not None
    owned = await claim(current.config_id)
    assert owned is not None
    return owned


async def connected(f: ChannelDatabase) -> ZaloConnection:
    owned = await claimed(f)
    await write_auth(owned, "opaque-session")
    await handle_frame(owned, Identity(type="identity", id="identity", token="test", account_id="123456789"))
    await handle_frame(owned, Ready(type="ready", id="ready", token="test"))
    current = await get_personal(f.db.agent.id)
    assert current is not None
    return current


async def bind(f: ChannelDatabase, owned: ZaloConnection) -> None:
    await f.db.first.execute(
        "INSERT INTO channel_sender_bindings(config_id,provider,provider_account_id,external_user_id,user_id,tenant_id) "
        "VALUES(%(config)s,'zalo',%(account)s,'987654321',%(user)s,%(tenant)s)",
        {"config": owned.config_id, "account": owned.account_id, "user": f.sender_id, "tenant": f.tenant_id},
    )


def route_for(owned: ZaloConnection) -> ZaloRoute:
    assert owned.account_id is not None
    return ZaloRoute(
        config_id=owned.config_id,
        auth_epoch=owned.auth_epoch,
        generation=owned.generation,
        account_id=owned.account_id,
        peer_id="987654321",
    )


def message_for() -> Message:
    return Message(
        type="message",
        id="message",
        token="test",
        message_id="incoming-1",
        sender_id="987654321",
        thread_id="987654321",
        from_me=False,
        timestamp=int(datetime.now(UTC).timestamp()),
        text="hello",
    )
