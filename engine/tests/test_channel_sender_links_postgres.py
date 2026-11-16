from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from tests.channel_submission_fixtures import channel_db, receipt_db
from tests.test_channel_event_receipts_postgres import counts

from app.api import channel_links, telegram
from app.core.domain.channels import ChannelType
from app.core.security import load_user_principal
from app.dao import channel_config_dao
from app.dao.channel_sender_link_dao import channel_sender_link_dao
from app.db.session import get_connection, transaction
from app.records.zalo_personal import ZaloSource
from app.services.channel_user_service import ChannelUserResolutionError
from app.services.channels import sender_links, submissions, telegram_setup
from app.services.platform_service import platform_service

__all__ = ["channel_db", "receipt_db"]


@pytest.fixture
async def linked_channel(channel_db):
    fixture = channel_db
    async with transaction(fixture.db.first):
        await fixture.db.first.execute("UPDATE channel_configs SET channel_type = 'telegram'")
        config = await channel_config_dao.update(
            db_obj=replace(fixture.config, channel_type=ChannelType.TELEGRAM),
            obj_in={
                "app_id": "123456",
                "app_secret": "123456:secret",
                "encrypt_key": None,
                "verification_token": "hook-secret",
            },
        )
    return replace(
        fixture,
        config=config,
        event=replace(
            fixture.event, provider="telegram", provider_account_id="123456", conversation_id="telegram_15550001"
        ),
    )


async def member(fixture):
    return await load_user_principal(fixture.sender_id, fresh=True)


async def issue(fixture):
    return await sender_links.issue_link_code(fixture.event.agent_id, "telegram", await member(fixture))


def command(fixture, code, *, key="link-1", sender="15550001"):
    return replace(fixture.event, event_key=key, sender_id=sender, text=f"/link {code}")


async def test_issue_consume_status_accept_and_unlink_over_real_routes(linked_channel):
    fixture = linked_channel
    app = FastAPI()
    app.include_router(channel_links.router, prefix="/api")
    app.include_router(telegram.router, prefix="/api")
    async with transaction(fixture.db.first):
        actor = await member(fixture)
    app.dependency_overrides[channel_links.get_current_user] = lambda: actor
    path = f"/api/agents/{fixture.event.agent_id}/channel-links/telegram"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async with transaction(fixture.db.first):
            status = await client.get(path)
            assert status.status_code == 200
            assert status.json() == {
                "channel": "telegram",
                "available": True,
                "linked": False,
                "linked_at": None,
                "external_hint": None,
            }
            issued = await client.post(path + "/code")
            assert issued.status_code == 200
            assert issued.headers["cache-control"] == "no-store, private"
            body = issued.json()
            assert len(body["code"]) == 10
            assert body["deep_link"] is None
            ttl = datetime.fromisoformat(body["expires_at"]) - datetime.now(UTC)
            assert timedelta(seconds=590) < ttl <= timedelta(seconds=600)
            stored = await fixture.db.first.fetchone("SELECT * FROM channel_link_codes")
            assert stored is not None
            assert body["code"] not in repr(stored)
            assert len(stored["code_hash"]) == 64
        message = {
            "message_id": 1,
            "chat": {"id": 15550001, "type": "private"},
            "from": {"id": 15550001, "is_bot": False},
            "text": "/link " + body["code"],
        }
        hook = f"/api/channel/telegram/{fixture.config.id}/webhook"
        async with transaction(fixture.db.first):
            response = await client.post(
                hook,
                json={"update_id": 1, "message": message},
                headers={"X-Telegram-Bot-Api-Secret-Token": "hook-secret"},
            )
            assert response.status_code == 200
            assert response.json()["method"] == "sendMessage"
            assert response.json()["chat_id"] == "15550001"
        state = await counts(fixture)
        assert state["receipts"] == 1
        assert state["enqueued"] == 0
        assert state["charged"] == 0
        async with transaction(fixture.db.first):
            status = await client.get(path)
            assert status.json()["linked"] is True
            assert status.json()["linked_at"] is not None
            assert status.json()["external_hint"] == "***0001"
            accepted = await submissions.accept_channel_event(fixture.event)
            assert not accepted.replayed
        state = await counts(fixture)
        assert state["receipts"] == 2
        assert state["enqueued"] == 1
        assert state["charged"] == 1
        async with transaction(fixture.db.first):
            deleted = await client.delete(path)
            assert deleted.status_code == 204
            assert not deleted.content
            assert deleted.headers["cache-control"] == "no-store, private"
        with pytest.raises(ChannelUserResolutionError):
            async with transaction(fixture.db.first):
                await submissions.accept_channel_event(replace(fixture.event, event_key="after-unlink"))
    app.dependency_overrides.clear()


async def test_expiry_normalization_reissue_and_replay(linked_channel, monkeypatch):
    fixture = linked_channel
    monkeypatch.setattr(sender_links.secrets, "randbits", lambda _bits: int("0000100001", 32))
    async with transaction(fixture.db.first):
        first = await issue(fixture)
        assert sender_links.normalize_code("ooooL ooooI") == first.code
        assert await sender_links.consume_link_command(command(fixture, first.code), "ooooL-ooooI") == "linked"
        assert await sender_links.consume_link_command(command(fixture, first.code), first.code) == "replayed"
        assert (
            await sender_links.consume_link_command(command(fixture, first.code, key="other"), first.code) == "replayed"
        )
    monkeypatch.setattr(sender_links.secrets, "randbits", lambda _bits: 33)
    async with transaction(fixture.db.first):
        expired = await issue(fixture)
        await fixture.db.first.execute(
            "UPDATE channel_link_codes SET expires_at = now() - interval '1 second' WHERE consumed_at IS NULL"
        )
        assert (
            await sender_links.consume_link_command(command(fixture, expired.code, key="expired"), expired.code)
            == "invalid"
        )
    monkeypatch.setattr(sender_links.secrets, "randbits", lambda _bits: 34)
    async with transaction(fixture.db.first):
        current = await issue(fixture)
        assert (
            await sender_links.consume_link_command(command(fixture, expired.code, key="old"), expired.code)
            == "invalid"
        )
        assert (
            await sender_links.consume_link_command(command(fixture, current.code, key="new"), current.code)
            == "already_linked"
        )
        assert await fixture.db.first.fetchval("SELECT count(*) FROM channel_event_receipts") == 2


async def test_external_ownership_conflict_and_replace_own_previous_sender(linked_channel):
    fixture = linked_channel
    async with transaction(fixture.db.first):
        code = await issue(fixture)
        assert await sender_links.consume_link_command(command(fixture, code.code), code.code) == "linked"
        creator = await load_user_principal(fixture.db.user.id, fresh=True)
        other = await sender_links.issue_link_code(fixture.event.agent_id, "telegram", creator)
        assert (
            await sender_links.consume_link_command(command(fixture, other.code, key="conflict"), other.code)
            == "conflict"
        )
        assert (
            await sender_links.consume_link_command(
                command(fixture, other.code, key="creator", sender="222"), other.code
            )
            == "linked"
        )
        own = await issue(fixture)
        assert (
            await sender_links.consume_link_command(command(fixture, own.code, key="replace", sender="333"), own.code)
            == "linked"
        )
        assert not await sender_links.is_bound(fixture.config.id, "123456", "15550001")
        assert await sender_links.is_bound(fixture.config.id, "123456", "333")
        assert await sender_links.is_bound(fixture.config.id, "123456", "222")


@pytest.mark.parametrize("scope", ["account", "config", "group", "zalo_source"])
async def test_codes_cannot_cross_source_scopes(linked_channel, scope):
    fixture = linked_channel
    async with transaction(fixture.db.first):
        issued = await issue(fixture)
        event = command(fixture, issued.code)
        if scope == "account":
            await fixture.db.first.execute("UPDATE channel_configs SET app_id = '999'")
            event = replace(event, provider_account_id="999")
            assert await sender_links.consume_link_command(event, issued.code) == "invalid"
        elif scope == "config":
            event = replace(event, config_id=uuid4())
            with pytest.raises(HTTPException):
                await sender_links.consume_link_command(event, issued.code)
        else:
            event = replace(event, is_group=True) if scope == "group" else replace(event, provider="zalo")
            with pytest.raises(HTTPException):
                await sender_links.consume_link_command(event, issued.code)
        assert await fixture.db.first.fetchval("SELECT count(*) FROM channel_sender_bindings") == 0


@pytest.mark.parametrize("revoked", ["user", "tenant", "permission", "membership", "password", "identity"])
async def test_issue_consume_and_inbound_recheck_fresh_authority(linked_channel, revoked):
    fixture = linked_channel
    async with transaction(fixture.db.first):
        actor = await member(fixture)
        issued = await issue(fixture)
        assert await sender_links.consume_link_command(command(fixture, issued.code), issued.code) == "linked"
        fresh = await issue(fixture)
        if revoked in {"password", "identity"}:
            identity = uuid4()
            await fixture.db.first.execute(
                "INSERT INTO identities(id, email, is_active, must_change_password) VALUES (%(id)s, %(email)s, %(active)s, %(password)s)",
                {
                    "id": identity,
                    "email": f"{identity}@test.invalid",
                    "active": revoked != "identity",
                    "password": revoked == "password",
                },
            )
            await fixture.db.first.execute(
                "UPDATE users SET identity_id = %(identity)s WHERE id = %(user)s",
                {"identity": identity, "user": fixture.sender_id},
            )
        else:
            statements = {
                "user": "UPDATE users SET is_active = FALSE",
                "tenant": "UPDATE tenants SET is_active = FALSE",
                "permission": "UPDATE agents SET access_mode = 'private'",
                "membership": "UPDATE users SET tenant_id = NULL",
            }
            await fixture.db.first.execute(statements[revoked])
        assert (
            await sender_links.consume_link_command(command(fixture, fresh.code, key="revoked"), fresh.code)
            == "invalid"
        )
        with pytest.raises(HTTPException):
            await sender_links.issue_link_code(fixture.event.agent_id, "telegram", actor)
        with pytest.raises(HTTPException):
            await submissions._authorize(fixture.event)


async def test_concurrent_code_redemption_creates_one_binding_and_receipt(linked_channel, monkeypatch):
    fixture = linked_channel
    async with transaction(fixture.db.first):
        issued = await issue(fixture)
    locked, attempted, release = anyio.Event(), anyio.Event(), anyio.Event()
    lock = channel_sender_link_dao.lock_scope
    outcomes = []

    async def gated(config_id, account):
        if get_connection() is fixture.db.second:
            attempted.set()
        await lock(config_id, account)
        if get_connection() is fixture.db.first:
            locked.set()
            await release.wait()

    monkeypatch.setattr(channel_sender_link_dao, "lock_scope", gated)

    async def redeem(connection, sender):
        async with transaction(connection):
            outcomes.append(
                await sender_links.consume_link_command(
                    command(fixture, issued.code, key=sender, sender=sender), issued.code
                )
            )

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(redeem, fixture.db.first, "111")
            await locked.wait()
            tasks.start_soon(redeem, fixture.db.second, "222")
            await attempted.wait()
            while not await fixture.db.first.fetchval(
                "SELECT cardinality(pg_blocking_pids(%(pid)s))", {"pid": fixture.db.second.raw.info.backend_pid}
            ):
                await anyio.lowlevel.checkpoint()
            release.set()
    assert outcomes == ["linked", "replayed"]
    async with transaction(fixture.db.first):
        assert await fixture.db.first.fetchval("SELECT count(*) FROM channel_sender_bindings") == 1
        assert await fixture.db.first.fetchval("SELECT count(*) FROM channel_event_receipts") == 1
        assert await sender_links.is_bound(fixture.config.id, "123456", "111")


async def test_binding_failure_rolls_back_code_and_receipt(linked_channel, monkeypatch):
    fixture = linked_channel
    async with transaction(fixture.db.first):
        issued = await issue(fixture)
    bind = channel_sender_link_dao.bind

    async def fail(**kwargs):
        await bind(**kwargs)
        raise RuntimeError("binding failed")

    monkeypatch.setattr(channel_sender_link_dao, "bind", fail)
    with pytest.raises(RuntimeError, match="binding failed"):
        async with transaction(fixture.db.first):
            await sender_links.consume_link_command(command(fixture, issued.code), issued.code)
    async with transaction(fixture.db.first):
        assert await fixture.db.first.fetchval("SELECT count(*) FROM channel_sender_bindings") == 0
        assert await fixture.db.first.fetchval("SELECT count(*) FROM channel_event_receipts") == 0
        assert await fixture.db.first.fetchval("SELECT consumed_at FROM channel_link_codes") is None


async def test_redemption_holds_authority_until_transaction_commits(linked_channel, monkeypatch):
    fixture = linked_channel
    async with transaction(fixture.db.first):
        issued = await issue(fixture)
    authorized, release = anyio.Event(), anyio.Event()
    reserve = submissions.channel_event_receipt_dao.reserve
    outcomes = []

    async def pause(receipt):
        authorized.set()
        await release.wait()
        return await reserve(receipt)

    monkeypatch.setattr(submissions.channel_event_receipt_dao, "reserve", pause)

    async def redeem():
        async with transaction(fixture.db.first):
            outcomes.append(await sender_links.consume_link_command(command(fixture, issued.code), issued.code))

    async def revoke():
        from app.core.acl_locks import agent_acl_mutation

        async with transaction(fixture.db.second), agent_acl_mutation(fixture.event.agent_id):
            await fixture.db.second.execute("UPDATE agents SET access_mode = 'private'")
            outcomes.append("revoked")

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(redeem)
            await authorized.wait()
            tasks.start_soon(revoke)
            while not await fixture.db.first.fetchval(
                "SELECT cardinality(pg_blocking_pids(%(pid)s))", {"pid": fixture.db.second.raw.info.backend_pid}
            ):
                await anyio.lowlevel.checkpoint()
            release.set()
    assert outcomes == ["linked", "revoked"]
    with pytest.raises(HTTPException):
        async with transaction(fixture.db.first):
            await submissions._authorize(fixture.event)


async def test_telegram_account_swap_invalidates_links_and_open_codes(linked_channel, monkeypatch):
    fixture = linked_channel
    async with transaction(fixture.db.first):
        issued = await issue(fixture)
        assert await sender_links.consume_link_command(command(fixture, issued.code), issued.code) == "linked"
        _ = await issue(fixture)
        creator = await load_user_principal(fixture.db.user.id, fresh=True)
    monkeypatch.setattr(telegram, "require_channel_creator", AsyncMock())
    monkeypatch.setattr(platform_service, "get_public_base_url", AsyncMock(return_value="https://engine.example"))
    from app.schemas.telegram import TelegramChannelConfigUpdate

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"ok": True, "result": True}))
    ) as provider:
        monkeypatch.setattr(telegram_setup, "outbound_client", lambda: provider)
        for account in ("999", "123456"):
            async with transaction(fixture.db.first):
                await telegram.configure_telegram_channel(
                    fixture.event.agent_id,
                    TelegramChannelConfigUpdate(bot_token=f"{account}:secret"),
                    creator,
                )
    async with transaction(fixture.db.first):
        assert await fixture.db.first.fetchval("SELECT count(*) FROM channel_sender_bindings") == 0
        assert await fixture.db.first.fetchval("SELECT count(*) FROM channel_link_codes") == 0


async def test_unbound_sender_cannot_use_legacy_org_member_mapping(linked_channel):
    fixture = linked_channel
    with pytest.raises(ChannelUserResolutionError):
        async with transaction(fixture.db.first):
            await submissions._authorize(fixture.event)


async def test_zalo_provider_aliases_share_one_native_receipt(linked_channel):
    fixture = linked_channel
    source = ZaloSource(fixture.config.id, uuid4(), uuid4(), uuid4(), "123456", "message-1")
    event = replace(fixture.event, provider="zalo", zalo_source=source, conversation_id="first-generation")
    canonical = replace(event, provider="zalo_personal", conversation_id="next-generation")
    first = submissions._receipt(event, fixture.db.agent)
    repeated = submissions._receipt(canonical, fixture.db.agent)
    async with transaction(fixture.db.first):
        assert await submissions.channel_event_receipt_dao.reserve(first)
        assert not await submissions.channel_event_receipt_dao.reserve(repeated)
        assert submissions._matching(await submissions.channel_event_receipt_dao.find(repeated), repeated)
        assert await fixture.db.first.fetchval("SELECT count(*) FROM channel_event_receipts") == 1
