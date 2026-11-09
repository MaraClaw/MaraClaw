from unittest.mock import AsyncMock
from uuid import uuid4

from app.core.domain.channels import ChannelType
from app.dao.channel_config_dao import channel_config_dao
from app.records.channel_config import ChannelConfigRecord


def connection_config(monkeypatch, agent_id, provider, *, app_id="app", credential="secret"):
    config = ChannelConfigRecord(
        id=uuid4(),
        agent_id=agent_id,
        channel_type=ChannelType(provider),
        app_id=app_id,
        app_secret=credential,
        is_configured=True,
        extra_config={
            "connection_mode": "gateway" if provider == "discord" else "websocket",
            "bot_id": "bot",
            "bot_secret": credential,
        },
    )
    monkeypatch.setattr(channel_config_dao, "get", AsyncMock(return_value=config))
    monkeypatch.setattr(channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    return config
