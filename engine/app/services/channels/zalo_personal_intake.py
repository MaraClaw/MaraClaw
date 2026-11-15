from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from app.dao.zalo_personal_dao import fenced
from app.db.session import connection_ctx
from app.records.outbound_plan import OutboundChunk
from app.records.zalo_personal import ZaloConnection, ZaloRoute, ZaloSource
from app.services.channels.zalo_personal_protocol import Message
from app.services.channels.zalo_personal_source import authorize_zalo_source


async def intake(connection: ZaloConnection, frame: Message) -> None:
    from app.services.channels import sender_links
    from app.services.channels.submissions import ChannelEvent, accept_channel_event
    from app.services.channels.zalo_personal_journal import queue_link_notice

    async with connection_ctx():
        current = await fenced(connection, connected=True)
        if (
            current.account_id is None
            or current.claim_token is None
            or current.activated_at is None
            or frame.from_me
            or frame.sender_id != frame.thread_id
            or frame.sender_id == current.account_id
            or frame.timestamp < int(current.activated_at.timestamp())
            or frame.timestamp > int(datetime.now(UTC).timestamp()) + 60
        ):
            return
        source = ZaloSource(
            current.config_id,
            current.auth_epoch,
            current.generation,
            current.claim_token,
            current.account_id,
            frame.message_id,
        )
        await authorize_zalo_source(source, current.agent_id)
        route = ZaloRoute(
            config_id=current.config_id,
            auth_epoch=current.auth_epoch,
            generation=current.generation,
            account_id=current.account_id,
            peer_id=frame.sender_id,
        )
        event = ChannelEvent(
            config_id=current.config_id,
            agent_id=current.agent_id,
            provider="zalo",
            event_key=f"personal:{current.auth_epoch}:{frame.message_id}",
            sender_id=frame.sender_id,
            conversation_id=route.external_id(),
            text=frame.text,
            provider_timestamp=datetime.fromtimestamp(frame.timestamp, UTC),
            provider_account_id=current.account_id,
            provider_timestamp_source="zalo_message",
            zalo_source=source,
        )
        command = sender_links.parse_link_command("zalo", frame.text)
        if command is not None:
            result = await sender_links.consume_link_command(event, command.code or "")
            match result:
                case "linked" | "already_linked" | "replayed":
                    notice = "Your Zalo account is linked. You can now message this agent."
                case "invalid":
                    notice = "This link code is invalid or expired. Get a new code in the web application."
                case "conflict":
                    notice = "This Zalo account is already linked to another member."
            await queue_link_notice(
                route,
                OutboundChunk(
                    key=uuid5(NAMESPACE_URL, f"zalo-link:{current.config_id}:{current.auth_epoch}:{frame.message_id}"),
                    text=notice,
                ),
            )
            return
        if not await sender_links.is_bound(current.config_id, current.account_id, frame.sender_id):
            return
        await accept_channel_event(event)
