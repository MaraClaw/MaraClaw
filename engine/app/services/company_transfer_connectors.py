from uuid import UUID

from app.config import get_settings
from app.process_roles import parse_process_roles, role_enabled


async def stop_free_transfer_connectors(agent_id: UUID) -> str | None:
    from app.services.dingtalk_stream import dingtalk_stream_manager
    from app.services.discord_gateway import discord_gateway_manager
    from app.services.feishu_ws import feishu_ws_manager
    from app.services.wechat_channel import wechat_poll_manager
    from app.services.wecom_stream import wecom_stream_manager

    await feishu_ws_manager.stop_client(agent_id)
    await dingtalk_stream_manager.stop_client(agent_id)
    await wecom_stream_manager.stop_client(agent_id)
    await wechat_poll_manager.stop_client(agent_id)
    await discord_gateway_manager.stop_client(agent_id)
    if dingtalk_stream_manager.status().get(str(agent_id), False):
        return "connector_still_running"
    # API/worker singletons cannot attest that the separate connector owner stopped.
    if not role_enabled(parse_process_roles(get_settings().PROCESS_ROLE), "connector"):
        return "connector_cleanup_pending"
    return None
