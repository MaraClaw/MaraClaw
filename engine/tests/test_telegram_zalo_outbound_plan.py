import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal, assert_never
from uuid import UUID, uuid4

import httpx
import pytest

from app.core.domain.channels import ChannelType
from app.dao import zalo_personal_dao
from app.records.channel_config import ChannelConfigRecord
from app.records.outbound_plan import OutboundChunk, OutboundPlan, OutboundProvider
from app.records.zalo_personal import ZaloConnection, ZaloRoute
from app.services.channels import outbound_identity, outbound_plan, telegram_outbound
from app.services.channels.outbound_identity import preflight_outbound
from app.services.channels.outbound_transport import send_chunk
from app.services.channels.whatsapp_personal_journal import PersonalSendPendingError, PersonalSendTerminalError
from gateway_report_relay_fakes import RelayStore

TOKEN = "123456:private_bot-token"


@pytest.fixture
def telegram_store(monkeypatch: pytest.MonkeyPatch) -> RelayStore:
    store = RelayStore()
    store.session.source_channel = "telegram"
    store.session.external_conv_id = "telegram_42"
    store.config.channel_type = ChannelType.TELEGRAM
    store.config.app_id = "123456"
    store.config.app_secret = TOKEN

    async def config(agent_id: UUID, *, channel_type: ChannelType) -> ChannelConfigRecord:
        assert agent_id == store.source.id
        assert channel_type == store.config.channel_type
        return store.config

    monkeypatch.setattr(outbound_plan.channel_config_dao, "get_configured_for_agent", config)
    return store


@pytest.mark.parametrize("delta", [-1, 0, 1])
@pytest.mark.parametrize("character", ["x", "\u754c", "\U0001f642"])
async def test_telegram_plan_chunks_by_codepoints_and_roundtrips(
    telegram_store: RelayStore, delta: int, character: str
) -> None:
    configured = await outbound_plan.configured_outbound(telegram_store.source, telegram_store.session)
    text = character * (4096 + delta)

    plan = outbound_plan.build_plan(configured.target, text, uuid4())

    assert tuple(chunk.text for chunk in plan.chunks) == ((text,) if delta <= 0 else (character * 4096, character))
    assert plan.target.account == ("123456",)
    assert plan.target.destination == "42"
    encoded = plan.model_dump_json()
    assert TOKEN not in encoded
    assert OutboundPlan.model_validate_json(encoded) == plan


@pytest.mark.parametrize(
    "external", ["", "42", "telegram_", "telegram_-42", "telegram_0", "telegram_@user", "telegram_42_extra"]
)
async def test_telegram_plan_refuses_nonprivate_or_malformed_routes(telegram_store: RelayStore, external: str) -> None:
    telegram_store.session.external_conv_id = external

    with pytest.raises(ValueError):
        await outbound_plan.configured_outbound(telegram_store.source, telegram_store.session)


@pytest.mark.parametrize("field", ["app_id", "app_secret"])
async def test_telegram_plan_requires_configured_identity_and_credentials(
    telegram_store: RelayStore, field: str
) -> None:
    setattr(telegram_store.config, field, None)

    with pytest.raises(ValueError):
        await outbound_plan.configured_outbound(telegram_store.source, telegram_store.session)


async def test_telegram_rotation_preserves_account_but_replacement_changes_target(telegram_store: RelayStore) -> None:
    first = await outbound_plan.configured_outbound(telegram_store.source, telegram_store.session)
    telegram_store.config.app_secret = "123456:rotated-token"
    rotated = await outbound_plan.configured_outbound(telegram_store.source, telegram_store.session)
    assert rotated.target == first.target

    telegram_store.config.app_secret = "654321:replacement-token"
    with pytest.raises(ValueError):
        await outbound_plan.configured_outbound(telegram_store.source, telegram_store.session)

    telegram_store.config.app_id = "654321"
    replaced = await outbound_plan.configured_outbound(telegram_store.source, telegram_store.session)
    assert replaced.target != first.target


async def test_telegram_send_chunk_uses_provider_helper_and_acknowledgment(
    telegram_store: RelayStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = await outbound_plan.configured_outbound(telegram_store.source, telegram_store.session)
    text = " \n\u4e16\u754c\U0001f642 " * 1000
    plan = outbound_plan.build_plan(configured.target, text, uuid4())
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(requests)}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(telegram_outbound, "outbound_client", lambda: client)

        def unexpected_http(**kwargs: str) -> httpx.AsyncClient:
            pytest.fail("Configured account preflight attempted external identity lookup")

        with monkeypatch.context() as preflight:
            preflight.setattr(outbound_identity.httpx, "AsyncClient", unexpected_http)
            proved = await preflight_outbound(configured)
        acknowledged = [await send_chunk(proved.snapshot, chunk) for chunk in plan.chunks]

    assert proved.target == configured.target
    assert acknowledged == ["1", "2"]
    assert [json.loads(request.content) for request in requests] == [
        {"chat_id": "42", "text": chunk.text} for chunk in plan.chunks
    ]
    assert all(request.url.raw_path == f"/bot{TOKEN}/sendMessage".encode() for request in requests)


@pytest.fixture
def zalo_state(telegram_store: RelayStore, monkeypatch: pytest.MonkeyPatch) -> tuple[RelayStore, ZaloConnection]:
    store = telegram_store
    store.session.source_channel = ChannelType.ZALO
    store.config.channel_type = ChannelType.ZALO
    store.config.extra_config = {"mode": "personal_qr"}
    connection = ZaloConnection(
        config_id=store.config.id,
        agent_id=store.source.id,
        tenant_id=uuid4(),
        owner_id=uuid4(),
        auth_epoch=uuid4(),
        generation=uuid4(),
        attempt_id=uuid4(),
        revision=1,
        state="connected",
        desired=True,
        created_at=datetime.now(UTC),
        account_id="123",
        claim_token=uuid4(),
        lease_until=datetime.now(UTC) + timedelta(hours=1),
    )
    store.session.external_conv_id = ZaloRoute(
        config_id=connection.config_id,
        auth_epoch=connection.auth_epoch,
        generation=connection.generation,
        account_id="123",
        peer_id="456",
    ).external_id()

    async def current(agent_id: UUID) -> ZaloConnection:
        assert agent_id == store.source.id
        return connection

    monkeypatch.setattr(zalo_personal_dao, "get_personal", current)
    return store, connection


@pytest.mark.parametrize("delta", [-1, 0, 1])
@pytest.mark.parametrize("character", ["x", "\u754c", "\U0001f642"])
async def test_zalo_plan_preserves_fences_and_codepoint_boundaries(
    zalo_state: tuple[RelayStore, ZaloConnection], delta: int, character: str
) -> None:
    store, connection = zalo_state
    configured = await outbound_plan.configured_outbound(store.source, store.session)
    text = character * (2000 + delta)

    plan = outbound_plan.build_plan(configured.target, text, uuid4())

    assert tuple(chunk.text for chunk in plan.chunks) == ((text,) if delta <= 0 else (character * 2000, character))
    assert plan.target.mode == "personal_qr"
    assert plan.target.destination == store.session.external_conv_id
    assert plan.target.account == ("123", str(connection.auth_epoch), str(connection.generation))
    assert OutboundPlan.model_validate_json(plan.model_dump_json()) == plan


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda c: None, id="missing"),
        pytest.param(lambda c: replace(c, config_id=uuid4()), id="connection-config"),
        pytest.param(lambda c: replace(c, auth_epoch=uuid4()), id="epoch"),
        pytest.param(lambda c: replace(c, generation=uuid4()), id="generation"),
        pytest.param(lambda c: replace(c, account_id="999"), id="account"),
        pytest.param(lambda c: replace(c, desired=False), id="disabled"),
        pytest.param(lambda c: replace(c, state="reconnecting"), id="disconnected"),
        pytest.param(lambda c: replace(c, claim_token=None), id="unclaimed"),
        pytest.param(lambda c: replace(c, lease_until=None), id="no-lease"),
        pytest.param(lambda c: replace(c, lease_until=c.created_at - timedelta(seconds=1)), id="expired-lease"),
    ],
)
async def test_zalo_plan_refuses_stale_connections(
    zalo_state: tuple[RelayStore, ZaloConnection],
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[ZaloConnection], ZaloConnection | None],
) -> None:
    store, connection = zalo_state

    async def current(agent_id: UUID) -> ZaloConnection | None:
        return mutate(connection)

    monkeypatch.setattr(zalo_personal_dao, "get_personal", current)
    with pytest.raises(PersonalSendTerminalError, match=r"^personal_route_revoked$"):
        await outbound_plan.configured_outbound(store.source, store.session)


@pytest.mark.parametrize("change", ["invalid", "config", "mode", "missing-config"])
async def test_zalo_plan_refuses_invalid_routes_and_modes(
    zalo_state: tuple[RelayStore, ZaloConnection],
    monkeypatch: pytest.MonkeyPatch,
    change: Literal["invalid", "config", "mode", "missing-config"],
) -> None:
    store, _ = zalo_state
    match change:
        case "invalid":
            store.session.external_conv_id = "zalo_456"
        case "config":
            store.config.id = uuid4()
        case "mode":
            store.config.extra_config = {"mode": "cloud"}
        case "missing-config":

            async def missing(agent_id: UUID, *, channel_type: ChannelType) -> None:
                return None

            monkeypatch.setattr(outbound_plan.channel_config_dao, "get_configured_for_agent", missing)
        case unreachable:
            assert_never(unreachable)

    with pytest.raises(PersonalSendTerminalError):
        await outbound_plan.configured_outbound(store.source, store.session)


@pytest.mark.parametrize("outcome", ["accepted", "pending", "terminal"])
async def test_zalo_send_chunk_uses_its_durable_journal(
    zalo_state: tuple[RelayStore, ZaloConnection],
    monkeypatch: pytest.MonkeyPatch,
    outcome: Literal["accepted", "pending", "terminal"],
) -> None:
    from app.services.channels import zalo_personal_journal

    store, _ = zalo_state
    configured = await outbound_plan.configured_outbound(store.source, store.session)
    chunk = OutboundChunk(text=" \n\u754c\U0001f642 ", key=uuid4())
    journal: dict[UUID, tuple[str, str]] = {}

    async def enqueue(destination: str, part: OutboundChunk) -> None:
        journal[part.key] = (destination, part.text)
        match outcome:
            case "accepted":
                return
            case "pending":
                raise PersonalSendPendingError
            case "terminal":
                raise PersonalSendTerminalError("personal_send_uncertain")
            case unreachable:
                assert_never(unreachable)

    monkeypatch.setattr(zalo_personal_journal, "enqueue", enqueue)

    def unexpected_http(**kwargs: str) -> httpx.AsyncClient:
        pytest.fail("Configured account preflight attempted external identity lookup")

    monkeypatch.setattr(outbound_identity.httpx, "AsyncClient", unexpected_http)
    proved = await preflight_outbound(configured)
    if outcome == "accepted":
        assert await send_chunk(proved.snapshot, chunk) is None
    else:
        error = PersonalSendPendingError if outcome == "pending" else PersonalSendTerminalError
        with pytest.raises(error):
            await send_chunk(proved.snapshot, chunk)
    assert proved.target == configured.target
    assert journal == {chunk.key: (store.session.external_conv_id, chunk.text)}


@pytest.mark.parametrize("provider", [OutboundProvider.ZALO, OutboundProvider.TELEGRAM])
async def test_transport_forbids_zalo_cloud_and_other_personal_providers(
    telegram_store: RelayStore, provider: OutboundProvider
) -> None:
    configured = await outbound_plan.configured_outbound(telegram_store.source, telegram_store.session)
    target = configured.target.model_copy(
        update={"provider": provider, "mode": "cloud" if provider == OutboundProvider.ZALO else "personal_qr"}
    )

    with pytest.raises(PersonalSendTerminalError):
        await send_chunk(replace(configured, target=target), OutboundChunk(text="reply", key=uuid4()))
