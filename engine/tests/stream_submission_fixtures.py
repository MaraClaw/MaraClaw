from dataclasses import replace

from app.dao.channel_config_dao import channel_config_dao
from app.db.session import transaction
from app.services.channels.stream_source import stream_source


async def stream_fixture(fixture):
    async with transaction(fixture.db.first):
        await fixture.db.first.execute("UPDATE identity_providers SET provider_type = 'dingtalk'")
        await channel_config_dao.delete(record_id=fixture.config.id)
        config = await channel_config_dao.create(
            obj_in={
                "id": fixture.config.id,
                "agent_id": fixture.config.agent_id,
                "channel_type": "dingtalk",
                "app_id": fixture.config.app_id,
                "app_secret": fixture.config.app_secret,
                "is_configured": True,
                "extra_config": {"connection_mode": "websocket"},
            }
        )
    event = replace(
        fixture.event,
        provider="dingtalk",
        conversation_id="dingtalk_p2p_15550001",
        provider_timestamp=None,
        provider_account_id=None,
        stream_source=stream_source(config),
    )
    return replace(fixture, config=config, event=event)
