"""Invitation destination preview, mounted under ``/tenants``."""

from fastapi import APIRouter, Depends

from app.core.security import get_current_user
from app.records.user import UserRecord
from app.schemas.company_admission import AdmissionTenant, InvitationPreviewRequest, InvitationPreviewResponse
from app.services.company_admission_auto import preview_invitation

router = APIRouter(prefix="/invitations", tags=["tenants"])


@router.post("/preview", response_model=InvitationPreviewResponse)
async def preview_invitation_destination(
    data: InvitationPreviewRequest, current_user: UserRecord = Depends(get_current_user)
) -> InvitationPreviewResponse:
    """Name the company a code admits the caller to. Read-only; nothing is consumed or moved."""
    target = await preview_invitation(current_user, data.invitation_code)
    return InvitationPreviewResponse(tenant=AdmissionTenant(id=target.id, name=target.name))
