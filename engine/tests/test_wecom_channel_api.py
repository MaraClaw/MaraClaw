import uuid
from datetime import UTC, datetime

import pytest

from app.api import wecom as wecom_api
from app.records.agent import AgentRecord
from app.records.channel_config import ChannelConfigRecord
from app.records.user import UserRecord


def make_user(**overrides):
    values = {
        "id": uuid.uuid4(),
        "identity_id": uuid.uuid4(),
        "display_name": "Alice",
        "role": "member",
        "tenant_id": uuid.uuid4(),
        "is_active": True,
    }
    values.update(overrides)
    return UserRecord(**values)


def make_channel(agent_id: uuid.UUID, *, connection_mode: str = "websocket") -> ChannelConfigRecord:
    return ChannelConfigRecord(
        id=uuid.uuid4(),
        agent_id=agent_id,
        channel_type="wecom",
        app_id="corp_id",
        app_secret="secret",
        is_configured=True,
        is_connected=False,
        extra_config={"connection_mode": connection_mode, "bot_id": "bot_123", "bot_secret": "secret_123"},
        created_at=datetime.now(UTC),
    )


def test_wecom_signature_matches_protocol_sha1_vector():
    # Given: fixed callback-signature inputs from the WeCom signing algorithm
    token = "token"
    timestamp = "1700000000"
    nonce = "n0nce"
    encrypted_message = "ciphertext"

    # When: generating the callback signature
    signature = wecom_api._verify_signature(token, timestamp, nonce, encrypted_message)

    # Then: it matches the protocol-defined SHA-1 digest
    assert signature == "9b510b8c2e9c9f83e86714aca1395343d5735640"


def test_parse_wecom_xml_reads_valid_callback_payload():
    # Given: a valid encrypted WeCom callback payload
    payload = b"<xml><Encrypt>ciphertext</Encrypt></xml>"

    # When: parsing the untrusted callback XML at the boundary
    root = wecom_api._parse_wecom_xml(payload)

    # Then: the required encrypted message can be read
    assert root.findtext("Encrypt") == "ciphertext"


def test_parse_wecom_xml_preserves_empty_element_findtext_default():
    # Given: a callback payload with an empty direct child element
    payload = b"<xml><Content /></xml>"

    # When: parsing the callback XML
    root = wecom_api._parse_wecom_xml(payload)

    # Then: findtext retains the ElementTree default-value contract
    assert root.findtext("Content") is None
    assert root.findtext("Content", "fallback") == "fallback"


def test_parse_wecom_xml_rejects_doctype_payload():
    # Given: XML carrying a DTD/entity payload
    payload = b'<!DOCTYPE xml [<!ENTITY test "expanded">]><xml><Encrypt>&test;</Encrypt></xml>'

    # When / Then: the parser rejects unsupported DTD processing
    with pytest.raises(ValueError, match="DOCTYPE"):
        wecom_api._parse_wecom_xml(payload)


@pytest.mark.asyncio
async def test_get_wecom_channel_reports_runtime_websocket_status(monkeypatch):
    agent_id = uuid.uuid4()
    config = make_channel(agent_id, connection_mode="websocket")

    async def fake_check_agent_access(_user, _agent_id, _db=None):
        return AgentRecord(id=agent_id, name="Agent", creator_id=uuid.uuid4()), None

    async def fake_get_for_agent(*, agent_id: uuid.UUID, channel_type: str):
        assert channel_type == "wecom"
        assert agent_id == config.agent_id
        return config

    class FakeManager:
        def status(self):
            return {str(agent_id): True}

    monkeypatch.setattr(wecom_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(wecom_api, "wecom_stream_manager", FakeManager())
    monkeypatch.setattr(wecom_api.channel_config_dao, "get_for_agent", fake_get_for_agent)

    result = await wecom_api.get_wecom_channel(
        agent_id=agent_id,
        current_user=make_user(),
    )

    assert result.is_connected is True


@pytest.mark.asyncio
async def test_get_wecom_channel_marks_webhook_mode_disconnected(monkeypatch):
    agent_id = uuid.uuid4()
    config = make_channel(agent_id, connection_mode="webhook")

    async def fake_check_agent_access(_user, _agent_id, _db=None):
        return AgentRecord(id=agent_id, name="Agent", creator_id=uuid.uuid4()), None

    async def fake_get_for_agent(*, agent_id: uuid.UUID, channel_type: str):
        assert channel_type == "wecom"
        assert agent_id == config.agent_id
        return config

    monkeypatch.setattr(wecom_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(wecom_api.channel_config_dao, "get_for_agent", fake_get_for_agent)

    result = await wecom_api.get_wecom_channel(
        agent_id=agent_id,
        current_user=make_user(),
    )

    assert result.is_connected is False


@pytest.mark.asyncio
async def test_delete_wecom_channel_stops_runtime_client(monkeypatch):
    agent_id = uuid.uuid4()
    config = make_channel(agent_id)
    stop_calls = []
    deleted_ids: list[uuid.UUID] = []
    creator = make_user()

    async def fake_check_agent_access(_user, _agent_id, _db=None):
        return AgentRecord(id=agent_id, name="Agent", creator_id=creator.id), None

    async def fake_stop_client(aid):
        stop_calls.append(aid)

    async def fake_get_for_agent(*, agent_id: uuid.UUID, channel_type: str):
        assert channel_type == "wecom"
        assert agent_id == config.agent_id
        return config

    async def fake_delete(*, id: uuid.UUID):
        deleted_ids.append(id)
        return config

    monkeypatch.setattr(wecom_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr("app.services.wecom_stream.wecom_stream_manager.stop_client", fake_stop_client)
    monkeypatch.setattr(wecom_api.channel_config_dao, "get_for_agent", fake_get_for_agent)
    monkeypatch.setattr(wecom_api.channel_config_dao, "delete", fake_delete)

    await wecom_api.delete_wecom_channel(
        agent_id=agent_id,
        current_user=creator,
    )

    assert stop_calls == [agent_id]
    assert deleted_ids == [config.id]
