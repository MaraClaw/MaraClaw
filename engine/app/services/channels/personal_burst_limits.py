"""Personal WhatsApp burst limits as the persistence layer's typed record, projected from validated settings."""

from app.config import Settings, get_settings
from app.records.personal_burst import PersonalBurstLimits


def personal_burst_limits(settings: Settings | None = None) -> PersonalBurstLimits:
    configured = settings if settings is not None else get_settings()
    return PersonalBurstLimits(
        quiet_seconds=configured.WHATSAPP_PERSONAL_BURST_QUIET_SECONDS,
        max_seconds=configured.WHATSAPP_PERSONAL_BURST_MAX_SECONDS,
        pending_count=configured.WHATSAPP_PERSONAL_BURST_PENDING_COUNT,
        pending_bytes=configured.WHATSAPP_PERSONAL_BURST_PENDING_BYTES,
        batch_bytes=configured.WHATSAPP_PERSONAL_BURST_BODY_BYTES,
        notice_cooldown_seconds=configured.WHATSAPP_PERSONAL_BUSY_NOTICE_COOLDOWN_SECONDS,
    )
