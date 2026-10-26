"""Provider-time evidence for free-form WhatsApp session replies."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, override

from app.core.json_types import JsonValue
from app.dao.channel_event_receipt_dao import channel_event_receipt_dao
from app.records.channel_event_receipt import ChannelReplyScope
from app.services.channels.outbound_plan import ConfiguredOutbound

type WhatsAppWindowReason = Literal["whatsapp_window_unavailable", "whatsapp_window_expired"]


@dataclass(slots=True)
class WhatsAppReplyWindowError(Exception):
    reason: WhatsAppWindowReason

    @override
    def __str__(self) -> str:
        return self.reason


def provider_timestamp(value: JsonValue, now: datetime) -> datetime | None:
    """Accept only provider Unix-second strings that were not future at receipt."""
    match value:
        case str() as seconds if seconds.isascii() and seconds.isdecimal() and len(seconds) <= 12:
            try:
                timestamp = datetime.fromtimestamp(int(seconds), UTC)
            except OverflowError, OSError, ValueError:
                return None
            return timestamp if timestamp <= now else None
        case _:
            return None


def utcnow() -> datetime:
    return datetime.now(UTC)


async def require_reply_window(outbound: ConfiguredOutbound, clock: Callable[[], datetime]) -> None:
    target, config = outbound.target, outbound.config
    account = config.app_id or ""
    if target.config_id != config.id or target.account != (account,):
        raise WhatsAppReplyWindowError("whatsapp_window_unavailable")
    timestamp = await channel_event_receipt_dao.latest_eligible_timestamp(
        ChannelReplyScope(
            config.id, config.agent_id, outbound.tenant_id, "whatsapp", account, f"whatsapp_{target.destination}"
        )
    )
    now = clock()
    if timestamp is None or timestamp > now:
        raise WhatsAppReplyWindowError("whatsapp_window_unavailable")
    if now - timestamp >= timedelta(hours=24):
        raise WhatsAppReplyWindowError("whatsapp_window_expired")
