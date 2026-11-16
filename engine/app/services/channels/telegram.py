from datetime import UTC, datetime

from app.records.channel_config import ChannelConfigRecord
from app.schemas.telegram import TelegramUpdate
from app.services.channels.submissions import ChannelEvent
from app.services.channels.telegram_media import message_media


def telegram_event(config: ChannelConfigRecord, update: TelegramUpdate) -> ChannelEvent | None:
    message = update.message
    if message is None or message.chat.type != "private" or message.chat.id <= 0 or message.sender_chat is not None:
        return None
    sender = message.sender
    if sender is None or sender.is_bot or str(sender.id) == config.app_id or sender.id != message.chat.id:
        return None
    media = message_media(message)
    text = message.text or message.caption or ""
    if not text.strip() and media is None:
        return None
    return ChannelEvent(
        config_id=config.id,
        agent_id=config.agent_id,
        provider="telegram",
        event_key=f"{config.app_id}:{message.chat.id}:{message.message_id}",
        sender_id=str(sender.id),
        conversation_id=f"telegram_{message.chat.id}",
        text=text,
        kind=media[0] if media is not None else "text",
        media_ids=(media[1].file_id,) if media is not None else (),
        provider_timestamp=datetime.fromtimestamp(message.date, UTC) if message.date is not None else None,
        provider_timestamp_source=str(message.date) if message.date is not None else None,
        provider_account_id=config.app_id,
    )
