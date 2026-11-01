"""Terminal Personal burst rejections: a durable outcome plus at most one static notice per scope cooldown."""

from typing import Final

from app.dao.personal_burst_input_dao import personal_burst_input_dao
from app.dao.personal_busy_notice_dao import personal_busy_notice_dao
from app.records.personal_burst import PersonalBurstLimits
from app.records.personal_burst_input import RejectedOutcome
from app.records.personal_burst_notice import BusyNoticeReason
from app.services.channels.personal_burst_gateway import create_notice_source
from app.services.channels.personal_burst_original import PersonalOriginal
from app.services.channels.personal_burst_outcome import PersonalRejected

_REASON: Final[dict[RejectedOutcome, BusyNoticeReason]] = {
    "capacity_rejected": "capacity",
    "oversize_rejected": "oversize",
    "quota_rejected": "quota",
}
# Every text says the message was NOT queued and to resend later; only the capacity text mentions a waiting backlog.
_NOTICE: Final[dict[BusyNoticeReason, str]] = {
    "capacity": "Your message was not queued because too many messages are already waiting. Please resend it later.",
    "oversize": "Your message was not queued because it is too long. Please send a shorter message later.",
    "quota": "Your message was not queued because your message quota has been reached. Please resend it later.",
}


async def reject_personal_original(
    original: PersonalOriginal, outcome: RejectedOutcome, limits: PersonalBurstLimits
) -> PersonalRejected:
    """Persist the rejection (no body kept). Runs in the caller's transaction under the shared-session lock."""
    covering = await personal_busy_notice_dao.find_recent(original.scope, limits.notice_cooldown_seconds)
    if covering is not None:
        _ = await personal_burst_input_dao.record_rejection(original.receipt, outcome, notice_id=covering.id)
        return PersonalRejected(outcome, covering.id, notice_created=False)
    reason = _REASON[outcome]
    source = await create_notice_source(original, _NOTICE[reason])
    notice = await personal_busy_notice_dao.create_notice(original.scope, source.id, reason)
    _ = await personal_burst_input_dao.record_rejection(original.receipt, outcome, notice_id=notice.id)
    return PersonalRejected(outcome, notice.id, notice_created=True)
