from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from tests.zalo_personal_fixtures import bind, channel_db, connected, message_for, receipt_db

from app.db.session import transaction
from app.records.zalo_personal import ZaloSource
from app.services.channels import sender_links
from app.services.channels.zalo_personal_intake import intake
from app.services.channels.zalo_personal_source import authorize_zalo_source

__all__ = ["channel_db", "receipt_db"]


async def test_linked_live_dm_is_durable_sender_work_and_replay_safe(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        await bind(f, owned)
    async with transaction(f.db.first):
        frame = message_for()
        await intake(owned, frame)
        await intake(owned, frame)
    async with transaction(f.db.second):
        assert await f.db.second.fetchval("SELECT count(*) FROM channel_event_receipts") == 1
        assert await f.db.second.fetchval("SELECT count(*) FROM gateway_messages") == 1
        assert await f.db.second.fetchval("SELECT sender_user_id FROM gateway_messages") == f.sender_id
        assert await f.db.second.fetchval("SELECT count(*) FROM personal_burst_inputs") == 0


@pytest.mark.parametrize(
    "change",
    [
        {"from_me": True},
        {"sender_id": "111"},
        {"sender_id": "123456789", "thread_id": "123456789"},
        {"timestamp": 1},
        {"timestamp": 9007199254740991},
    ],
)
async def test_self_group_history_and_future_messages_never_enter_intake(channel_db, change):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        await bind(f, owned)
        await intake(owned, message_for().model_copy(update=change))
        assert await f.db.first.fetchval("SELECT count(*) FROM channel_event_receipts") == 0
        assert await f.db.first.fetchval("SELECT count(*) FROM zalo_personal_sends") == 0


async def test_unbound_text_silent_and_link_command_queues_static_notice(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        await intake(owned, message_for())
        assert await f.db.first.fetchval("SELECT count(*) FROM zalo_personal_sends") == 0
        await intake(owned, message_for().model_copy(update={"text": "/link wrong-code"}))
        assert await f.db.first.fetchval("SELECT purpose FROM zalo_personal_sends") == "link_notice"
        assert await f.db.first.fetchval("SELECT count(*) FROM gateway_messages") == 0


async def test_zalo_source_fences_generation_claim_account_and_echo(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        assert owned.claim_token is not None
        assert owned.account_id is not None
        source = ZaloSource(
            owned.config_id, owned.auth_epoch, owned.generation, owned.claim_token, owned.account_id, "incoming"
        )
        await authorize_zalo_source(source, owned.agent_id)
        for stale in [
            replace(source, generation=uuid4()),
            replace(source, claim_token=uuid4()),
            replace(source, account_id="999"),
            replace(source, auth_epoch=uuid4()),
        ]:
            with pytest.raises(HTTPException):
                await authorize_zalo_source(stale, owned.agent_id)
        await f.db.first.execute(
            "INSERT INTO zalo_personal_sends(command_id,config_id,auth_epoch,generation,account_id,peer_id,text,state,message_id) "
            "VALUES(%(id)s,%(config)s,%(epoch)s,%(generation)s,%(account)s,'987654321','reply','accepted','incoming')",
            {
                "id": uuid4(),
                "config": owned.config_id,
                "epoch": owned.auth_epoch,
                "generation": owned.generation,
                "account": owned.account_id,
            },
        )
        with pytest.raises(HTTPException, match="echo"):
            await authorize_zalo_source(source, owned.agent_id)


async def test_real_member_link_code_consumes_without_llm_work(channel_db):
    from app.core.security import load_user_principal

    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        user = await load_user_principal(f.sender_id, fresh=True)
        code = await sender_links.issue_link_code(owned.agent_id, "zalo", user)
        await intake(owned, message_for().model_copy(update={"text": "/link " + code.code}))
        assert await sender_links.is_bound(owned.config_id, "123456789", "987654321")
        assert await f.db.first.fetchval("SELECT purpose FROM zalo_personal_sends") == "link_notice"
        assert await f.db.first.fetchval("SELECT count(*) FROM gateway_messages") == 0
