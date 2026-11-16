from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import anyio
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from tests.channel_submission_fixtures import ChannelDatabase
from tests.zalo_personal_fixtures import bind, channel_db, connected, message_for, receipt_db

from app.config import Settings, get_settings
from app.db.errors import DbError
from app.db.session import transaction
from app.records.zalo_personal import ZaloConnection, ZaloRoute
from app.services.channels import zalo_personal_typing as typing
from app.services.channels.zalo_personal_intake import intake

__all__ = ["channel_db", "receipt_db"]


@pytest.fixture
def current(monkeypatch: pytest.MonkeyPatch) -> ZaloConnection:
    monkeypatch.setattr(get_settings(), "ZALO_PERSONAL_TYPING_ENABLED", True)
    return ZaloConnection(
        config_id=uuid4(),
        agent_id=uuid4(),
        tenant_id=uuid4(),
        owner_id=uuid4(),
        auth_epoch=uuid4(),
        generation=uuid4(),
        attempt_id=uuid4(),
        revision=1,
        state="connected",
        desired=True,
        created_at=datetime.now(UTC),
        claim_token=uuid4(),
        account_id="123456789",
        lease_until=datetime.now(UTC) + timedelta(minutes=5),
    )


@pytest.mark.parametrize("disabled", [True, False])
async def test_disabled_or_ineligible_never_opens_database(
    current: ZaloConnection, monkeypatch: pytest.MonkeyPatch, disabled: bool
) -> None:
    monkeypatch.setattr(get_settings(), "ZALO_PERSONAL_TYPING_ENABLED", not disabled)
    database = AsyncMock(side_effect=AssertionError("database accessed"))
    monkeypatch.setattr(typing, "connection_ctx", database)
    owned = current if disabled else replace(current, desired=False)
    assert await typing.TypingPump().due(owned) == []
    database.assert_not_called()


async def test_rate_is_per_peer_five_seconds_and_retires_inactive_work(
    current: ZaloConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    peers = [str(peer) for peer in range(8)]
    monkeypatch.setattr(typing, "busy_peers", AsyncMock(side_effect=lambda _: list(peers)))
    clock = [0.0]
    pump = typing.TypingPump(clock=lambda: clock[0])
    assert await pump.due(current) == peers
    clock[0] = 4.999
    assert await pump.due(current) == []
    peers[-1] = "9"
    assert await pump.due(current) == ["9"]
    clock[0] = 5.0
    assert await pump.due(current) == [str(peer) for peer in range(7)]
    peers.clear()
    assert await pump.due(current) == []
    assert pump._last_pulse == {}
    peers.append("0")
    assert await pump.due(current) == ["0"]


async def test_concurrent_ticks_emit_one_pulse(current: ZaloConnection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(typing, "busy_peers", AsyncMock(return_value=["987654321"]))
    pump = typing.TypingPump(clock=lambda: 0.0)
    results: list[list[str]] = []

    async def tick() -> None:
        results.append(await pump.due(current))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(tick)
        tasks.start_soon(tick)
    assert sorted(results) == [[], ["987654321"]]


@pytest.mark.parametrize("error", [DbError("secret"), HTTPException(409, "secret")])
async def test_optional_failures_emit_nothing_without_logging_payload(
    current: ZaloConnection, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(typing, "busy_peers", AsyncMock(side_effect=error))
    log = Mock()
    monkeypatch.setattr(typing, "logger", log)
    assert await typing.TypingPump().due(current) == []
    assert len(log.mock_calls) == 1
    assert "secret" not in str(log.mock_calls)


async def test_database_failure_does_not_reset_rate_or_hide_programmer_error(
    current: ZaloConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        typing, "busy_peers", AsyncMock(side_effect=[["987654321"], DbError("offline"), ["987654321"], TypeError()])
    )
    pump = typing.TypingPump(clock=lambda: 0.0)
    assert await pump.due(current) == ["987654321"]
    assert await pump.due(current) == []
    assert await pump.due(current) == []
    with pytest.raises(TypeError):
        await pump.due(current)


@pytest.mark.parametrize("cadence", [60, 300, 1800])
def test_settings_parse_operator_seconds_and_typing_opt_in(monkeypatch: pytest.MonkeyPatch, cadence: int) -> None:
    monkeypatch.setenv("ZALO_PERSONAL_TYPING_ENABLED", "true")
    monkeypatch.setenv("ZALO_PERSONAL_KEEPALIVE_INTERVAL_SECONDS", str(cadence))
    settings = Settings()
    assert settings.ZALO_PERSONAL_TYPING_ENABLED is True
    assert cadence == settings.ZALO_PERSONAL_KEEPALIVE_INTERVAL_SECONDS


@pytest.mark.parametrize("cadence", ["59", "1801", "1.5", "seconds"])
def test_settings_reject_invalid_operator_cadence(monkeypatch: pytest.MonkeyPatch, cadence: str) -> None:
    monkeypatch.setenv("ZALO_PERSONAL_KEEPALIVE_INTERVAL_SECONDS", cadence)
    with pytest.raises(ValidationError):
        Settings()


def test_settings_default_typing_off_and_keepalive_300_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZALO_PERSONAL_TYPING_ENABLED", raising=False)
    monkeypatch.delenv("ZALO_PERSONAL_KEEPALIVE_INTERVAL_SECONDS", raising=False)
    settings = Settings()
    assert settings.ZALO_PERSONAL_TYPING_ENABLED is False
    assert settings.ZALO_PERSONAL_KEEPALIVE_INTERVAL_SECONDS == 300


@pytest.fixture
async def typing_db(
    channel_db: ChannelDatabase, monkeypatch: pytest.MonkeyPatch
) -> tuple[ChannelDatabase, ZaloConnection]:
    monkeypatch.setattr(get_settings(), "ZALO_PERSONAL_TYPING_ENABLED", True)
    async with transaction(channel_db.db.first):
        owned = await connected(channel_db)
        await bind(channel_db, owned)
        await intake(owned, message_for())
    return channel_db, owned


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE gateway_messages SET status='completed'",
        "UPDATE gateway_messages SET status='cancelled'",
        "UPDATE gateway_messages SET created_at=now()-interval '10 minutes 1 second'",
        "UPDATE gateway_messages SET created_at=now()+interval '1 minute'",
        "UPDATE gateway_report_deliveries SET delivered_at=now()",
        "UPDATE gateway_report_deliveries SET terminal_at=now(),terminal_reason='payload_rejected'",
        "UPDATE gateway_report_deliveries SET destination_kind='agent'",
        "UPDATE chat_sessions SET is_group=TRUE WHERE source_channel='zalo'",
        "UPDATE chat_sessions SET external_conv_id=external_conv_id || ':group' WHERE source_channel='zalo'",
        "DELETE FROM channel_sender_bindings",
        "UPDATE agents SET status='stopped'",
        "UPDATE tenants SET is_active=FALSE",
        "UPDATE users SET is_active=FALSE",
        "UPDATE zalo_personal_connections SET lease_until=now()-interval '1 second'",
        "UPDATE zalo_personal_connections SET desired=FALSE",
        "UPDATE zalo_personal_connections SET state='reconnecting'",
        "UPDATE zalo_personal_connections SET claim_token=gen_random_uuid()",
        "UPDATE zalo_personal_connections SET auth_epoch=gen_random_uuid()",
        "UPDATE zalo_personal_connections SET generation=gen_random_uuid()",
        "UPDATE zalo_personal_connections SET account_id='999'",
    ],
)
async def test_real_busy_reply_stops_after_completion_or_revocation(
    typing_db: tuple[ChannelDatabase, ZaloConnection], statement: str
) -> None:
    f, owned = typing_db
    pump = typing.TypingPump(clock=lambda: 0.0)
    async with transaction(f.db.first):
        assert await pump.due(owned) == ["987654321"]
    async with transaction(f.db.first):
        await f.db.first.execute(statement)
    async with transaction(f.db.first):
        assert await typing.TypingPump().due(owned) == []


@pytest.mark.parametrize("field", ["config_id", "auth_epoch", "generation", "account_id"])
async def test_real_stale_route_fences_never_pulse(
    typing_db: tuple[ChannelDatabase, ZaloConnection], field: str
) -> None:
    f, owned = typing_db
    assert owned.account_id is not None
    route = ZaloRoute(
        config_id=owned.config_id,
        auth_epoch=owned.auth_epoch,
        generation=owned.generation,
        account_id=owned.account_id,
        peer_id="987654321",
    )
    stale = route.model_copy(update={field: "999" if field == "account_id" else uuid4()})
    async with transaction(f.db.first):
        await f.db.first.execute(
            "UPDATE chat_sessions SET external_conv_id=%(route)s WHERE source_channel='zalo'",
            {"route": stale.external_id()},
        )
        assert await typing.TypingPump().due(owned) == []


async def test_real_valid_peers_not_starved_by_unbound_or_stale_destinations(
    typing_db: tuple[ChannelDatabase, ZaloConnection],
) -> None:
    f, owned = typing_db
    assert owned.account_id is not None
    async with transaction(f.db.first):
        for index in range(30):
            peer, member, session, message = str(1000 + index), uuid4(), uuid4(), uuid4()
            route = ZaloRoute(
                config_id=owned.config_id,
                auth_epoch=owned.auth_epoch,
                generation=owned.generation,
                account_id=owned.account_id,
                peer_id=peer,
            )
            if index < 10:
                route = route.model_copy(update={"generation": uuid4()})
            await f.db.first.execute(
                "INSERT INTO users(id,tenant_id,display_name) VALUES(%(id)s,%(tenant)s,'typing')",
                {"id": member, "tenant": f.tenant_id},
            )
            if index < 10 or index >= 20:
                await f.db.first.execute(
                    "INSERT INTO channel_sender_bindings"
                    "(config_id,provider,provider_account_id,external_user_id,user_id,tenant_id) "
                    "VALUES(%(config)s,'zalo',%(account)s,%(peer)s,%(member)s,%(tenant)s)",
                    {
                        "config": owned.config_id,
                        "account": owned.account_id,
                        "peer": peer,
                        "member": member,
                        "tenant": f.tenant_id,
                    },
                )
            await f.db.first.execute(
                "INSERT INTO chat_sessions(id,agent_id,user_id,title,source_channel,external_conv_id) "
                "VALUES(%(id)s,%(agent)s,%(member)s,'typing','zalo',%(route)s)",
                {"id": session, "agent": owned.agent_id, "member": member, "route": route.external_id()},
            )
            await f.db.first.execute(
                "INSERT INTO gateway_messages(id,agent_id,conversation_id,content,status,created_at) "
                "VALUES(%(id)s,%(agent)s,'shared','work','delivered',now()-interval '1 minute')",
                {"id": message, "agent": owned.agent_id},
            )
            await f.db.first.execute(
                "INSERT INTO gateway_report_deliveries(id,message_id,destination_kind,destination_id,tenant_id) "
                "VALUES(%(id)s,%(message)s,'im',%(session)s,%(tenant)s)",
                {"id": uuid4(), "message": message, "session": session, "tenant": f.tenant_id},
            )
        pump = typing.TypingPump(clock=lambda: 0.0)
        assert await pump.due(owned) == [str(peer) for peer in range(1020, 1028)]
        assert await pump.due(owned) == []
