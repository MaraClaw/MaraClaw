import json
from collections.abc import Awaitable, Callable

import httpx
import pytest
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao as dao
from app.db.session import get_connection, transaction
from app.services import gateway_report_relay as relay
from app.services.wechat_channel import WECHAT_CONTEXT_CACHE_KEY
from test_channel_delivery_transport import transport
from test_gateway_report_delivery_postgres import delivery_db as delivery_db, receipt_db as receipt_db
from test_gateway_report_relay_postgres import relay_db as relay_db


@pytest.fixture(params=["slack", "discord", "wechat"])
async def credential_db(relay_db, request):
    db, row = relay_db
    provider = request.param
    limit = 4000 if provider == "slack" else 2000
    extra = {"bot_token": "stale-duplicate-token", WECHAT_CONTEXT_CACHE_KEY: {"peer": {"context_token": "context"}}}
    async with transaction(db.first):
        await db.first.execute(
            "UPDATE channel_configs SET channel_type = %(provider)s, app_id = %(app)s, extra_config = %(extra)s "
            "WHERE agent_id = %(agent)s",
            {
                "provider": provider,
                "app": "slack" if provider == "slack" else "declared-bot",
                "extra": Jsonb(extra),
                "agent": db.agent.id,
            },
        )
        await db.first.execute(
            "UPDATE chat_sessions SET source_channel = %(provider)s, external_conv_id = %(external)s WHERE id = %(id)s",
            {"provider": provider, "external": f"{provider}_peer", "id": db.session_id},
        )
        await db.first.execute(
            "UPDATE gateway_messages SET result = %(text)s WHERE id = %(id)s",
            {"text": "a" * limit + "last chunk", "id": row.message_id},
        )
    return db, row, provider


class CredentialProvider:
    def __init__(self, db, provider):
        self.db, self.provider = db, provider
        self.principal = "1"
        self.proofs: list[str] = []
        self.messages: list[tuple[str, str]] = []
        self.on_proof: Callable[[], Awaitable[None]] | None = None

    async def respond(self, request):
        assert get_connection() is None
        assert self.db.first.raw.info.transaction_status == TransactionStatus.IDLE
        credential = request.headers["Authorization"]
        if request.url.path in {"/api/auth.test", "/api/v10/users/@me"}:
            self.proofs.append(credential)
            if self.on_proof is not None:
                await self.on_proof()
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "team_id": "T1",
                    "bot_id": f"B{self.principal}",
                    "id": f"bot-{self.principal}",
                    "bot": True,
                },
            )
        body = json.loads(request.content)
        text = (
            body["msg"]["item_list"][0]["text_item"]["text"]
            if self.provider == "wechat"
            else body.get("text", body.get("content"))
        )
        self.messages.append((credential, text))
        return httpx.Response(200, json={"ok": True, "id": "ack", "ret": 0})


async def change_credentials(db, row, token, *, app_id=None):
    async with transaction(db.second):
        await db.second.execute(
            "UPDATE channel_configs SET app_secret = %(token)s, app_id = COALESCE(%(app)s, app_id) WHERE agent_id = %(agent)s",
            {"token": token, "app": app_id, "agent": db.agent.id},
        )
        await db.second.execute(
            "UPDATE gateway_report_deliveries SET available_at = clock_timestamp() WHERE id = %(id)s", {"id": row.id}
        )


async def test_same_principal_credential_rotation_resumes_persisted_cursor(credential_db, monkeypatch):
    db, row, provider = credential_db
    wire = CredentialProvider(db, provider)
    transport(monkeypatch, wire.respond)
    assert await relay.relay_batch() == 1
    async with transaction(db.second):
        original = await dao.get(row.id)
    await change_credentials(db, row, "rotated-secret")

    assert await relay.relay_batch() == 1

    async with transaction(db.second):
        delivered = await dao.get(row.id)
    assert original is not None
    assert delivered is not None
    assert delivered.im_plan == original.im_plan
    assert delivered.next_chunk == 2
    assert delivered.delivered_at is not None
    prefix = "Bot" if provider == "discord" else "Bearer"
    assert wire.messages == [
        (f"{prefix} live-secret", "a" * (4000 if provider == "slack" else 2000)),
        (f"{prefix} rotated-secret", "last chunk"),
    ]
    assert original.im_plan is not None
    assert original.im_plan.target.account == (
        ("slack", "T1", "B1")
        if provider == "slack"
        else ("declared-bot", "bot-1")
        if provider == "discord"
        else ("declared-bot",)
    )


async def test_different_principal_is_blocked_until_original_account_is_repaired(credential_db, monkeypatch):
    db, row, provider = credential_db
    wire = CredentialProvider(db, provider)
    transport(monkeypatch, wire.respond)
    assert await relay.relay_batch() == 1
    wire.principal = "2"
    await change_credentials(db, row, "other-bot-token", app_id="other-bot" if provider == "wechat" else None)

    assert await relay.relay_batch() == 1

    async with transaction(db.second):
        blocked = await dao.get(row.id)
    assert blocked is not None
    assert blocked.next_chunk == 1
    assert blocked.delivered_at is None
    assert blocked.terminal_at is None
    assert len(wire.messages) == 1
    wire.principal = "1"
    await change_credentials(db, row, "repaired-secret", app_id="declared-bot" if provider == "wechat" else None)
    assert await relay.relay_batch() == 1
    assert wire.messages[-1][1] == "last chunk"


async def test_missing_canonical_credentials_remain_repairable_after_ten_thousand_attempts(credential_db, monkeypatch):
    db, row, provider = credential_db
    wire = CredentialProvider(db, provider)
    transport(monkeypatch, wire.respond)
    assert await relay.relay_batch() == 1
    await change_credentials(db, row, None)
    async with transaction(db.second):
        await db.second.execute(
            "UPDATE gateway_report_deliveries SET attempts = 10000 WHERE id = %(id)s", {"id": row.id}
        )

    assert await relay.relay_batch() == 1

    async with transaction(db.second):
        blocked = await dao.get(row.id)
    assert blocked is not None
    assert blocked.next_chunk == 1
    assert blocked.terminal_at is None
    assert len(wire.messages) == 1
    await change_credentials(db, row, "repaired-secret")
    assert await relay.relay_batch() == 1
    assert wire.messages[-1][1] == "last chunk"


@pytest.mark.parametrize("provider", ["slack", "discord"])
async def test_configuration_change_during_preflight_cannot_send_unproved_credential(relay_db, monkeypatch, provider):
    db, row = relay_db
    async with transaction(db.second):
        await db.second.execute(
            "UPDATE channel_configs SET channel_type = %(provider)s WHERE agent_id = %(id)s",
            {"provider": provider, "id": db.agent.id},
        )
        await db.second.execute(
            "UPDATE chat_sessions SET source_channel = %(provider)s, external_conv_id = %(external)s WHERE id = %(id)s",
            {"provider": provider, "external": f"{provider}_peer", "id": db.session_id},
        )
    wire = CredentialProvider(db, provider)

    async def rotate_during_proof():
        await change_credentials(db, row, "unproved-token")

    wire.on_proof = rotate_during_proof
    transport(monkeypatch, wire.respond)

    assert await relay.relay_batch() == 1

    async with transaction(db.second):
        blocked = await dao.get(row.id)
    assert wire.messages == []
    assert blocked is not None
    assert blocked.im_plan is None
    assert blocked.next_chunk == 0
    wire.on_proof = None
    await change_credentials(db, row, "unproved-token")
    assert await relay.relay_batch() == 1
    assert wire.proofs == [
        f"{'Bot' if provider == 'discord' else 'Bearer'} live-secret",
        f"{'Bot' if provider == 'discord' else 'Bearer'} unproved-token",
    ]
    assert wire.messages == [(wire.proofs[-1], "result")]


async def test_config_rotation_after_validation_sends_only_the_proved_snapshot(relay_db, monkeypatch):
    db, row = relay_db
    wire = CredentialProvider(db, "slack")
    transport(monkeypatch, wire.respond)
    send = relay.send_chunk

    async def rotate_before_send(outbound, chunk):
        await change_credentials(db, row, "unproved-token")
        await send(outbound, chunk)

    monkeypatch.setattr(relay, "send_chunk", rotate_before_send)

    assert await relay.relay_batch() == 1

    assert wire.proofs == ["Bearer live-secret"]
    assert wire.messages == [("Bearer live-secret", "result")]
