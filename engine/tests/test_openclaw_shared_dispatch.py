from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from app.db.connection import DbConnection
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.llm import LLMModelRecord
from app.services import openclaw_gateway_delivery as delivery, openclaw_native_claims as claims
from app.services.llm.turn import ModelBundle
from app.services.openclaw_guest_config import GuestConfigWriteError
from app.services.openclaw_model_selection import ResolvedModel
from app.services.openclaw_turns import runtime_session_key
from app.services.shared_chat import shared_session_id
from test_db_session import _FakeRawConnection, _wrap


def test_native_shared_session_preserves_existing_transcript_namespace() -> None:
    agent_id = uuid4()
    conversation = str(shared_session_id(agent_id))
    web = GatewayMessageRecord(id=uuid4(), agent_id=agent_id, conversation_id=conversation, content="web")
    im = replace(web, id=uuid4(), content="IM")
    group = replace(web, id=uuid4(), conversation_id=str(uuid4()))
    assert runtime_session_key(web) == runtime_session_key(im) == f"agent:main:hook:maraclaw:{conversation}"
    assert runtime_session_key(group) != runtime_session_key(web)


@pytest.mark.parametrize("conversation", [None, ""])
def test_unthreaded_native_messages_have_distinct_sessions(conversation: str | None) -> None:
    first = GatewayMessageRecord(id=uuid4(), agent_id=uuid4(), content="one", conversation_id=conversation)
    second = replace(first, id=uuid4(), content="two")
    assert runtime_session_key(first) == f"agent:main:hook:maraclaw:{first.id}"
    assert runtime_session_key(second) != runtime_session_key(first)


@pytest.fixture
def agent() -> AgentRecord:
    return AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4(), tenant_id=uuid4())


@pytest.fixture
def lock_order(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    order: list[str] = []
    db = _wrap(_FakeRawConnection())

    @asynccontextmanager
    async def transaction() -> AsyncGenerator[DbConnection]:
        order.append("begin")
        try:
            yield db
        except BaseException:
            order.append("rollback")
            raise
        else:
            order.append("commit")

    async def tenants(_db: DbConnection, _ids: object) -> None:
        order.append("tenant")

    async def owner(_db: DbConnection, _owner: UUID, _tenant: UUID | None) -> None:
        order.append("owner")

    async def agent_lock(_db: DbConnection, _agent: UUID, _tenant: UUID | None) -> None:
        order.append("agent")

    @asynccontextmanager
    async def guest(_agent: UUID) -> AsyncGenerator[None]:
        order.append("guest")
        yield None

    for module in (claims, delivery):
        monkeypatch.setattr(module, "connection_ctx", transaction)
        monkeypatch.setattr(module, "lock_tenants", tenants)
        monkeypatch.setattr(module, "lock_user", owner)
        monkeypatch.setattr(module, "lock_agent", agent_lock)
    monkeypatch.setattr(claims, "guest_write_section", guest)
    monkeypatch.setattr(claims, "get_connection", lambda: None)
    return order


async def test_ordinary_claim_uses_locked_scope_and_exact_model(
    agent: AgentRecord, lock_order: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    model = LLMModelRecord(uuid4(), "openai", "frozen", "key", "Selected")
    message = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="hi", guest_model_ref="openai/frozen")
    resolved = ResolvedModel(ModelBundle(primary=model), model)

    async def fresh(_id: UUID) -> AgentRecord:
        assert lock_order[-1] == "agent"
        lock_order.append("fresh")
        return agent

    async def claim(message_id: UUID, agent_id: UUID) -> GatewayMessageRecord:
        assert (message_id, agent_id) == (message.id, agent.id)
        lock_order.append("claim")
        return message

    apply = Mock()
    monkeypatch.setattr(claims, "load_agent_for_new_work", fresh)
    monkeypatch.setattr(claims.gateway_message_dao, "claim_ordinary_head", claim)
    resolve = AsyncMock(return_value=resolved)
    monkeypatch.setattr(claims, "resolve_stored_model", resolve)
    monkeypatch.setattr(claims, "apply_guest_model", apply)

    assert await claims.claim_ordinary_turn(agent, message.id) is message
    assert lock_order == ["guest", "begin", "tenant", "owner", "agent", "fresh", "claim", "commit"]
    resolve.assert_awaited_once_with(agent, "openai/frozen")
    apply.assert_called_once_with(agent, resolved.bundle, model)


@pytest.mark.parametrize("failure", ["missing-model", "config-write"])
async def test_claim_preparation_failure_rolls_back_delivery(
    agent: AgentRecord, lock_order: list[str], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    message = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="hi", guest_model_ref="openai/frozen")
    model = LLMModelRecord(uuid4(), "openai", "frozen", "key", "Selected")
    monkeypatch.setattr(claims, "load_agent_for_new_work", AsyncMock(return_value=agent))
    monkeypatch.setattr(claims.gateway_message_dao, "claim_ordinary_head", AsyncMock(return_value=message))
    monkeypatch.setattr(
        claims,
        "resolve_stored_model",
        AsyncMock(
            return_value=None if failure == "missing-model" else ResolvedModel(ModelBundle(primary=model), model)
        ),
    )
    monkeypatch.setattr(claims, "apply_guest_model", Mock(side_effect=GuestConfigWriteError()))
    with pytest.raises(GuestConfigWriteError):
        await claims.claim_ordinary_turn(agent, message.id)
    assert lock_order[-1] == "rollback"


@pytest.mark.parametrize("changed", ["owner", "tenant"])
async def test_ordinary_claim_rejects_changed_scope_before_message_lock(
    agent: AgentRecord, lock_order: list[str], monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    fresh = replace(agent, creator_id=uuid4()) if changed == "owner" else replace(agent, tenant_id=uuid4())
    monkeypatch.setattr(claims, "load_agent_for_new_work", AsyncMock(return_value=fresh))
    claim = AsyncMock()
    monkeypatch.setattr(claims.gateway_message_dao, "claim_ordinary_head", claim)
    assert await claims.claim_ordinary_turn(agent, uuid4()) is None
    claim.assert_not_awaited()


@pytest.mark.parametrize("scope", ["same", "owner-changed", "tenant-changed", "deleted"])
async def test_native_settlement_locks_scope_before_report_session(
    agent: AgentRecord, lock_order: list[str], monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    current = agent
    if scope == "owner-changed":
        current = replace(agent, creator_id=uuid4())
    elif scope == "tenant-changed":
        current = replace(agent, tenant_id=uuid4())
    monkeypatch.setattr(delivery.agent_dao, "get", AsyncMock(return_value=None if scope == "deleted" else current))
    message = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="hi", status="delivered")
    monkeypatch.setattr(delivery.gateway_message_dao, "get_for_agent", AsyncMock(return_value=message))
    report = delivery.RecordedReport(message, None)

    async def record(_agent: AgentRecord, _id: UUID, _text: str) -> delivery.RecordedReport:
        assert lock_order == ["begin", "tenant", "owner", "agent"]
        lock_order.append("session")
        return report

    record_call = AsyncMock(side_effect=record)
    monkeypatch.setattr(delivery, "record_report", record_call)
    result = await delivery.settle_native_report(agent, message.id, "answer")
    if scope == "same":
        assert result is report
        assert lock_order[-2:] == ["session", "commit"]
    else:
        assert result is None
        record_call.assert_not_awaited()


@pytest.mark.parametrize("status", ["pending", "completed"])
async def test_native_settlement_cannot_complete_unclaimed_or_already_completed_row(
    agent: AgentRecord, lock_order: list[str], monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    monkeypatch.setattr(delivery.agent_dao, "get", AsyncMock(return_value=agent))
    message = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="hi", status=status)
    monkeypatch.setattr(delivery.gateway_message_dao, "get_for_agent", AsyncMock(return_value=message))
    record = AsyncMock()
    monkeypatch.setattr(delivery, "record_report", record)
    assert await delivery.settle_native_report(agent, message.id, "answer") is None
    record.assert_not_awaited()
