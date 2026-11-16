from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from tests.test_telegram_zalo_outbound_plan import telegram_store, zalo_state

from app.core.domain.agents import AgentStatus
from app.dao import zalo_personal_dao
from app.records.outbound_plan import OutboundChunk
from app.records.user import UserRecord
from app.records.zalo_personal import SendState, ZaloConnection, ZaloSend
from app.services.channels import outbound_plan, sender_links, zalo_personal_journal as journal
from app.services.channels.whatsapp_personal_journal import PersonalSendPendingError, PersonalSendTerminalError
from gateway_report_relay_fakes import RelayStore

__all__ = ["telegram_store", "zalo_state"]


@dataclass
class JournalStore:
    connection: ZaloConnection | None
    rows: dict[UUID, ZaloSend] = field(default_factory=dict)
    inserts: int = 0
    owner: AsyncMock = field(default_factory=AsyncMock)
    bound: AsyncMock = field(default_factory=lambda: AsyncMock(return_value=True))
    fence: AsyncMock = field(default_factory=AsyncMock)

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[JournalStore]:
        yield self

    async def fetchone(self, query: str, params: Mapping[str, object]) -> ZaloConnection | ZaloSend | None:
        if "FROM zalo_personal_connections" in query:
            return self.connection
        assert "FROM zalo_personal_sends" in query
        if "state='pending'" in query:
            assert self.connection is not None
            assert params == {
                "config": self.connection.config_id,
                "epoch": self.connection.auth_epoch,
                "generation": self.connection.generation,
                "account": self.connection.account_id,
            }
            assert not any(row.state == "pending" for row in self.rows.values())
            return None
        return self.rows.get(UUID(str(params["id"])))

    async def execute(self, query: str, params: Mapping[str, object]) -> None:
        if query.startswith("SELECT pg_advisory_xact_lock"):
            return
        assert query.startswith("INSERT INTO zalo_personal_sends")
        assert "ON CONFLICT(command_id) DO NOTHING" in query
        self.inserts += 1
        row = ZaloSend.model_validate(
            {
                "command_id": params["command"],
                "config_id": params["config"],
                "auth_epoch": params["epoch"],
                "generation": params["generation"],
                "account_id": params["account"],
                "peer_id": params["peer"],
                "text": params["text"],
                "purpose": params["purpose"],
                "state": "pending",
            }
        )
        self.rows.setdefault(row.command_id, row)

    async def fetchval(self, query: str, params: Mapping[str, object]) -> None:
        assert "FROM zalo_personal_media_sends" in query


@pytest.fixture
def journal_store(zalo_state: tuple[RelayStore, ZaloConnection], monkeypatch: pytest.MonkeyPatch) -> JournalStore:
    store, connection = zalo_state
    store.source.tenant_id = connection.tenant_id
    store.source.creator_id = connection.owner_id
    db = JournalStore(connection)
    db.owner.return_value = (store.source, UserRecord(connection.owner_id, tenant_id=connection.tenant_id))

    async def current_fence(owned: ZaloConnection, *, connected: bool) -> ZaloConnection:
        assert connected
        assert db.connection is not None
        return db.connection

    db.fence.side_effect = current_fence
    monkeypatch.setattr(journal, "connection_ctx", db.session)
    monkeypatch.setattr(zalo_personal_dao, "connection_ctx", db.session)
    monkeypatch.setattr(journal, "zalo_owner", db.owner)
    monkeypatch.setattr(journal, "fenced", db.fence)
    monkeypatch.setattr(sender_links, "is_bound", db.bound)

    async def current(agent_id: UUID, *, lock: bool = False) -> ZaloConnection | None:
        assert agent_id == store.source.id
        return db.connection

    monkeypatch.setattr(zalo_personal_dao, "get_personal", current)
    return db


@pytest.mark.parametrize(
    "recover",
    [
        pytest.param(lambda c: replace(c, state="reconnecting", claim_token=None, lease_until=None), id="backoff"),
        pytest.param(lambda c: replace(c, state="reconnecting"), id="claimed-reconnect"),
        pytest.param(lambda c: replace(c, claim_token=None), id="unclaimed"),
        pytest.param(lambda c: replace(c, lease_until=None), id="missing-lease"),
        pytest.param(lambda c: replace(c, lease_until=c.created_at - timedelta(seconds=1)), id="expired-lease"),
    ],
)
async def test_completed_reply_waits_through_recovery_then_queues_same_plan(
    zalo_state: tuple[RelayStore, ZaloConnection],
    journal_store: JournalStore,
    recover: Callable[[ZaloConnection], ZaloConnection],
) -> None:
    store, connection = zalo_state
    configured = await outbound_plan.configured_outbound(store.source, store.session)
    plan = outbound_plan.build_plan(configured.target, "completed reply", uuid4())
    journal_store.connection = recover(connection)

    with pytest.raises(PersonalSendPendingError):
        await outbound_plan.configured_outbound(store.source, store.session)
    with pytest.raises(PersonalSendPendingError):
        await journal.enqueue(configured.target.destination, plan.chunks[0])
    assert journal_store.rows == {}
    assert journal_store.inserts == 0
    journal_store.owner.assert_awaited_with(store.source.id, authorization_only=True)
    journal_store.bound.assert_awaited_once_with(connection.config_id, connection.account_id, "456")

    journal_store.connection = replace(
        connection, claim_token=uuid4(), lease_until=datetime.now(UTC) + timedelta(seconds=30)
    )
    ready = await outbound_plan.configured_outbound(store.source, store.session)
    assert ready.target == plan.target
    with pytest.raises(PersonalSendPendingError):
        await journal.enqueue(ready.target.destination, plan.chunks[0])
    assert list(journal_store.rows) == [plan.chunks[0].key]
    assert journal_store.rows[plan.chunks[0].key].text == "completed reply"


@pytest.mark.parametrize("deny", ["unbound", "owner-auth", "owner-mismatch", "tenant-mismatch", "stopped", "self-peer"])
async def test_recovery_never_bypasses_sender_or_fresh_owner_authorization(
    zalo_state: tuple[RelayStore, ZaloConnection], journal_store: JournalStore, deny: str
) -> None:
    store, connection = zalo_state
    journal_store.connection = replace(connection, state="reconnecting", claim_token=None, lease_until=None)
    destination = store.session.external_conv_id
    assert destination is not None
    match deny:
        case "unbound":
            journal_store.bound.return_value = False
        case "owner-auth":
            journal_store.owner.side_effect = HTTPException(403, "Owner unavailable")
        case "owner-mismatch":
            journal_store.connection = replace(journal_store.connection, owner_id=uuid4())
        case "tenant-mismatch":
            journal_store.connection = replace(journal_store.connection, tenant_id=uuid4())
        case "stopped":
            store.source.status = AgentStatus.STOPPED
        case "self-peer":
            destination = destination.rsplit(":", 1)[0] + ":" + str(connection.account_id)
    with pytest.raises(PersonalSendTerminalError):
        await journal.enqueue(destination, OutboundChunk(key=uuid4(), text="private reply"))
    assert journal_store.rows == {}
    assert journal_store.inserts == 0


@pytest.mark.parametrize(
    ("change", "pending"),
    [
        pytest.param(
            lambda c: replace(c, state="reconnecting", claim_token=None, lease_until=None), True, id="recovering"
        ),
        pytest.param(lambda c: c, False, id="still-live"),
        pytest.param(lambda c: replace(c, state="reconnecting", generation=uuid4()), False, id="new-generation"),
        pytest.param(lambda c: replace(c, state="reconnecting", auth_epoch=uuid4()), False, id="new-epoch"),
        pytest.param(lambda c: replace(c, state="reconnecting", config_id=uuid4()), False, id="new-config"),
        pytest.param(lambda c: replace(c, state="reconnecting", account_id="999"), False, id="new-account"),
        pytest.param(lambda c: replace(c, state="reconnecting", desired=False), False, id="revoked"),
        pytest.param(lambda c: replace(c, state="reconnecting", purge_requested=True), False, id="purge"),
        pytest.param(lambda c: replace(c, state="reconnecting", owner_id=uuid4()), False, id="new-owner"),
        pytest.param(lambda c: None, False, id="missing"),
    ],
)
async def test_fenced_disconnect_race_rechecks_only_current_recovery(
    zalo_state: tuple[RelayStore, ZaloConnection],
    journal_store: JournalStore,
    change: Callable[[ZaloConnection], ZaloConnection | None],
    pending: bool,
) -> None:
    store, connection = zalo_state

    async def disconnect(owned: ZaloConnection, *, connected: bool) -> ZaloConnection:
        journal_store.connection = change(connection)
        raise HTTPException(409, "Zalo source revoked")

    journal_store.fence.side_effect = disconnect
    error = PersonalSendPendingError if pending else PersonalSendTerminalError
    with pytest.raises(error):
        await journal.enqueue(store.session.external_conv_id or "", OutboundChunk(key=uuid4(), text="raced reply"))
    assert journal_store.rows == {}
    assert journal_store.inserts == 0


@pytest.mark.parametrize("recovering", [False, True])
@pytest.mark.parametrize("state", ["accepted", "rejected", "uncertain", "cancelled", "dispatch_started"])
async def test_existing_journal_outcomes_survive_recovery_without_resending(
    zalo_state: tuple[RelayStore, ZaloConnection], journal_store: JournalStore, recovering: bool, state: SendState
) -> None:
    store, connection = zalo_state
    destination = store.session.external_conv_id
    assert destination is not None
    chunk = OutboundChunk(key=uuid4(), text="original reply")
    with pytest.raises(PersonalSendPendingError):
        await journal.enqueue(destination, chunk)
    journal_store.rows[chunk.key] = journal_store.rows[chunk.key].model_copy(update={"state": state})
    if recovering:
        journal_store.connection = replace(connection, state="reconnecting", claim_token=None, lease_until=None)
    if state == "accepted":
        await journal.enqueue(destination, chunk)
    else:
        error = PersonalSendPendingError if state == "dispatch_started" else PersonalSendTerminalError
        with pytest.raises(error, match=None if state == "dispatch_started" else "^personal_send_" + state + "$"):
            await journal.enqueue(destination, chunk)
    if recovering:
        assert journal_store.inserts == 1
    journal_store.connection = connection
    assert await journal.authorize_dispatch(connection) is None
    assert journal_store.rows[chunk.key].state == state
    assert len(journal_store.rows) == 1
