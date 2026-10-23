"""Private in-app welcomes for approved company admissions."""

from app.records.notification import NotificationRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services import notification_service


async def send_organization_welcome(user: UserRecord, tenant: TenantRecord) -> NotificationRecord | None:
    """Welcome only the applicant whose company admission was approved."""
    if user.is_platform_admin:
        return None
    return await notification_service.send_notification(
        user_id=user.id,
        type="organization_welcome",
        title=f"Welcome to {tenant.name}"[:200],
        body=f"You are now a member of {tenant.name}. Your organization's workspace is ready for you.",
        ref_id=tenant.id,
    )
