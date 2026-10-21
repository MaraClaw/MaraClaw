from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import anyio
import pytest

from app.records.agent import AgentRecord
from app.records.channel_config import ChannelConfigRecord
from app.records.chat import ChatSessionRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.gateway_report_delivery import GatewayReportDeliveryRecord, TerminalReason
from app.records.outbound_plan import OutboundChunk, OutboundPlan, OutboundTarget
from app.services import gateway_report_relay as relay
from app.services.channels.outbound_identity import OutboundPreflight
from app.services.channels.outbound_plan import ConfiguredOutbound
from app.services.channels.types import normalize_channel_type
from app.services.openclaw_routing import PreparedOpenClawMessage


class RelayStore:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 16, tzinfo=UTC)
        tenant = uuid4()
        self.source = AgentRecord(uuid4(), "source", uuid4(), tenant_id=tenant)
        self.destination = AgentRecord(uuid4(), "destination", uuid4(), tenant_id=tenant)
        self.session = ChatSessionRecord(
            uuid4(), self.source.id, uuid4(), source_channel="slack", external_conv_id="C1"
        )
        self.config = ChannelConfigRecord(uuid4(), self.source.id, app_id="account", app_secret="secret")
        self.message = GatewayMessageRecord(
            uuid4(),
            self.source.id,
            "request",
            status="completed",
            result="authoritative",
            sender_agent_id=self.destination.id,
            conversation_id=str(self.session.id),
        )
        self.rows: dict[UUID, GatewayReportDeliveryRecord] = {}
        self.inbox: list[GatewayMessageRecord] = []
        self.wakes: list[str] = []
        self.sends: list[str] = []
        self.transaction = ContextVar("relay_test_transaction", default=False)
        self.fail_ack = False
        self.fail_send = False
        self.crash_after_send = False
        self.preparation_elapsed = 0
        self.send_entered = anyio.Event()
        self.block_send = False
        self.on_prepare: Callable[[], None] = lambda: None
        self.on_lock: Callable[[], None] = lambda: None
        self.send_error: Exception | None = None
        self.block_enqueue = False
        self.enqueue_entered = anyio.Event()

    def clock(self) -> datetime:
        return self.now

    def intent(self, kind: str = "agent") -> GatewayReportDeliveryRecord:
        assert kind in {"agent", "im"}
        row = GatewayReportDeliveryRecord(
            uuid4(),
            self.message.id,
            "agent" if kind == "agent" else "im",
            self.destination.id if kind == "agent" else self.session.id,
            tenant_id=self.source.tenant_id,
            available_at=self.now,
        )
        self.rows[row.id] = row
        return row

    async def claim_batch(
        self, *, limit: int, lease_seconds: float, now: datetime
    ) -> list[GatewayReportDeliveryRecord]:
        due = [
            r
            for r in self.rows.values()
            if r.delivered_at is None
            and r.terminal_at is None
            and r.available_at is not None
            and r.available_at <= now
            and (r.lease_expires_at is None or r.lease_expires_at <= now)
        ][:limit]
        for row in due:
            row.claim_token = uuid4()
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.attempts += 1
        return [replace(row) for row in due]

    async def lock_claim(self, claim: GatewayReportDeliveryRecord, *, now: datetime) -> bool:
        row = self.rows.get(claim.id)
        return bool(
            row
            and claim.claim_token is not None
            and row.claim_token == claim.claim_token
            and row.delivered_at is None
            and row.terminal_at is None
            and row.lease_expires_at
            and row.lease_expires_at > now
        )

    async def acknowledge(self, claim: GatewayReportDeliveryRecord, *, now: datetime) -> bool:
        if self.fail_ack or not await self.lock_claim(claim, now=now):
            return False
        self.rows[claim.id].delivered_at = now
        self.rows[claim.id].claim_token = None
        self.rows[claim.id].lease_expires_at = None
        return True

    async def retry(self, claim: GatewayReportDeliveryRecord, *, available_at: datetime, now: datetime) -> bool:
        if not await self.lock_claim(claim, now=now):
            return False
        row = self.rows[claim.id]
        row.available_at, row.claim_token, row.lease_expires_at = available_at, None, None
        return True

    async def initialize_plan(self, claim: GatewayReportDeliveryRecord, plan: OutboundPlan, *, now: datetime) -> bool:
        row = self.rows[claim.id]
        if row.im_plan is not None or row.next_chunk != 0 or not await self.lock_claim(claim, now=now):
            return False
        row.im_plan = plan
        return True

    async def checkpoint_chunk(self, claim: GatewayReportDeliveryRecord, *, now: datetime) -> bool:
        row = self.rows[claim.id]
        if self.fail_ack or not await self.lock_claim(claim, now=now):
            return False
        if row.im_plan is None or row.next_chunk != claim.next_chunk or row.next_chunk >= len(row.im_plan.chunks):
            return False
        row.next_chunk += 1
        if row.next_chunk == len(row.im_plan.chunks):
            row.delivered_at = now
        row.available_at, row.claim_token, row.lease_expires_at = now, None, None
        return True

    async def finish_terminal(self, id: UUID, claim_token: UUID, reason: TerminalReason, *, now: datetime) -> bool:
        row = self.rows[id]
        claim = replace(row, claim_token=claim_token)
        if not await self.lock_claim(claim, now=now):
            return False
        row.terminal_reason, row.terminal_at = reason, now
        row.claim_token, row.lease_expires_at = None, None
        return True

    async def lock_identity(self, claim: GatewayReportDeliveryRecord) -> None:
        assert self.transaction.get()
        self.on_lock()

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[None]:
        assert not self.transaction.get()
        token = self.transaction.set(True)
        inbox, rows = self.inbox.copy(), {k: replace(v) for k, v in self.rows.items()}
        try:
            yield
        except BaseException:
            self.inbox, self.rows = inbox, rows
            raise
        else:
            self.wakes.extend(row.content for row in self.inbox[len(inbox) :])
        finally:
            self.transaction.reset(token)

    async def get_message(self, message_id: UUID) -> GatewayMessageRecord | None:
        return self.message if message_id == self.message.id else None

    async def get_agent(self, agent_id: UUID) -> AgentRecord | None:
        return next((a for a in (self.source, self.destination) if a.id == agent_id), None)

    async def get_agents(self, ids: Sequence[UUID]) -> list[AgentRecord]:
        return [agent for agent in (self.source, self.destination) if agent.id in ids]

    async def get_session(self, session_id: UUID) -> ChatSessionRecord | None:
        return self.session if session_id == self.session.id else None

    async def prepare(self, *, agent: AgentRecord, content: str) -> PreparedOpenClawMessage:
        assert not self.transaction.get()
        self.now += timedelta(seconds=self.preparation_elapsed)
        self.on_prepare()
        return PreparedOpenClawMessage(agent, content, "primary", None, None, "test")

    async def enqueue(
        self, prepared: PreparedOpenClawMessage, *, sender_agent_id: UUID, conversation_id: str | None = None
    ) -> GatewayMessageRecord:
        assert self.transaction.get()
        assert sender_agent_id == self.source.id
        row = GatewayMessageRecord(
            uuid4(),
            prepared.agent.id,
            prepared.content,
            sender_agent_id=sender_agent_id,
            conversation_id=conversation_id,
        )
        self.inbox.append(row)
        self.enqueue_entered.set()
        if self.block_enqueue:
            await anyio.sleep_forever()
        return row

    async def send(self, *, agent: AgentRecord, session: ChatSessionRecord, content: str, strict: bool = False) -> None:
        assert not self.transaction.get()
        assert strict
        assert agent.id == session.agent_id
        self.send_entered.set()
        if self.block_send:
            await anyio.sleep_forever()
        if self.fail_send:
            raise OSError("provider failure")
        if self.send_error is not None:
            raise self.send_error
        self.sends.append(content)
        if self.crash_after_send:
            self.now += timedelta(seconds=61)

    async def outbound(self, agent: AgentRecord, session: ChatSessionRecord) -> ConfiguredOutbound:
        target = OutboundTarget.model_validate(
            {
                "provider": normalize_channel_type(session.source_channel),
                "config_id": self.config.id,
                "account": [self.config.app_id or ""],
                "destination": session.external_conv_id,
            }
        )
        return ConfiguredOutbound(target, self.config)

    async def preflight(self, outbound: ConfiguredOutbound) -> OutboundPreflight:
        assert not self.transaction.get()
        return OutboundPreflight(deepcopy(outbound), outbound.target)

    async def send_chunk(self, outbound: ConfiguredOutbound, chunk: OutboundChunk) -> None:
        await self.send(agent=self.source, session=self.session, content=chunk.text, strict=True)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(relay, "gateway_report_delivery_dao", self)
        monkeypatch.setattr(relay.gateway_message_dao, "get", self.get_message)
        monkeypatch.setattr(relay.agent_dao, "get", self.get_agent)
        monkeypatch.setattr(relay.agent_dao, "get_many", self.get_agents)
        monkeypatch.setattr(relay.chat_session_dao, "get", self.get_session)
        monkeypatch.setattr(relay, "connection_ctx", self.connection)
        monkeypatch.setattr(relay, "_lock_identity", self.lock_identity)
        monkeypatch.setattr(relay, "prepare_openclaw_message", self.prepare)
        monkeypatch.setattr(relay, "enqueue_prepared_openclaw_message", self.enqueue)
        monkeypatch.setattr(relay, "configured_outbound", self.outbound)
        monkeypatch.setattr(relay, "preflight_outbound", self.preflight)
        monkeypatch.setattr(relay, "send_chunk", self.send_chunk)
