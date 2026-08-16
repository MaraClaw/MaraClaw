"""Inbound enqueue stub + gateway report IM delivery."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.records.agent import AgentRecord
from app.records.chat import ChatSessionRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services.channels.inbound import CHANNEL_REPLY_QUEUED, is_queued_channel_reply


def test_queued_stub_is_recognized() -> None:
    assert is_queued_channel_reply(CHANNEL_REPLY_QUEUED)
    assert not is_queued_channel_reply("Here is the real answer")


@pytest.mark.asyncio
async def test_report_result_scopes_ws_and_delivers_im(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import gateway
    from app.schemas.schemas import GatewayReportRequest

    agent = AgentRecord(id=uuid.uuid4(), creator_id=uuid.uuid4(), name="Bot", tenant_id=uuid.uuid4())
    session = ChatSessionRecord(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=uuid.uuid4(),
        source_channel="feishu",
        external_conv_id="feishu_p2p_ou_user",
    )
    msg = GatewayMessageRecord(
        id=uuid.uuid4(),
        agent_id=agent.id,
        content="hello",
        conversation_id=str(session.id),
        sender_user_id=session.user_id,
        status="pending",
    )
    sent: list[tuple[str, str, object]] = []
    delivered: list[tuple[object, object, str]] = []

    async def get_agent(_key: str, _db=None) -> AgentRecord:
        return agent

    async def get_for_agent(_message_id, _agent_id):
        return msg

    async def update(*, db_obj, obj_in):
        return db_obj

    async def insert_message(**_kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    async def get_session(_sid):
        return session

    async def get_participant(_type, _ref):
        return None

    class _Manager:
        async def send_to_session(self, agent_id: str, session_id: str, message: object) -> None:
            sent.append((agent_id, session_id, message))

    async def deliver_session_reply(*, agent, session, content: str) -> None:
        delivered.append((agent, session, content))

    monkeypatch.setattr(gateway, "_get_agent_by_key", get_agent)
    monkeypatch.setattr(gateway.gateway_message_dao, "get_for_agent", get_for_agent)
    monkeypatch.setattr(gateway.gateway_message_dao, "update", update)
    monkeypatch.setattr(gateway.agent_dao, "update", update)
    monkeypatch.setattr(gateway.chat_message_dao, "insert_message", insert_message)
    monkeypatch.setattr(gateway.chat_session_dao, "get", get_session)
    monkeypatch.setattr(gateway.participant_dao, "get_by_type_ref", get_participant)
    monkeypatch.setattr("app.api.websocket.manager", _Manager())
    monkeypatch.setattr("app.services.channels.outbound.deliver_session_reply", deliver_session_reply)

    result = await gateway.report_result(
        GatewayReportRequest(message_id=msg.id, result="Guest answer"),
        x_api_key="oc-test",
    )

    assert result == {"status": "ok"}
    assert sent == [
        (
            str(agent.id),
            str(session.id),
            {"type": "done", "role": "assistant", "content": "Guest answer"},
        )
    ]
    assert delivered == [(agent, session, "Guest answer")]


@pytest.mark.asyncio
async def test_deliver_session_reply_skips_web() -> None:
    from app.services.channels.outbound import deliver_session_reply

    agent = AgentRecord(id=uuid.uuid4(), creator_id=uuid.uuid4(), name="Bot")
    session = ChatSessionRecord(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=uuid.uuid4(),
        source_channel="web",
    )
    await deliver_session_reply(agent=agent, session=session, content="hi")
