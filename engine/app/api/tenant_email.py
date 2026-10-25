"""Organization SMTP API; saved configuration is the only source for tests."""

import smtplib
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from app.core.outbound_policy import OutboundPolicyError
from app.core.security import get_current_admin
from app.records.user import UserRecord
from app.schemas.tenant_email import TenantEmailOut, TenantEmailTestOut, TenantEmailUpdate
from app.services import tenant_email_admin

router = APIRouter(prefix="/enterprise/tenant-email", tags=["enterprise"])


@router.get("", response_model=TenantEmailOut)
async def get_tenant_email(
    tenant_id: UUID | None = None, current_user: UserRecord = Depends(get_current_admin)
) -> TenantEmailOut:
    return await tenant_email_admin.get_config(current_user, tenant_id)


@router.put("", response_model=TenantEmailOut)
async def put_tenant_email(
    data: TenantEmailUpdate, tenant_id: UUID | None = None, current_user: UserRecord = Depends(get_current_admin)
) -> TenantEmailOut:
    return await tenant_email_admin.put_config(current_user, tenant_id, data)


@router.delete("", status_code=204)
async def delete_tenant_email(
    tenant_id: UUID | None = None, current_user: UserRecord = Depends(get_current_admin)
) -> Response:
    await tenant_email_admin.delete_config(current_user, tenant_id)
    return Response(status_code=204)


@router.post("/test", response_model=TenantEmailTestOut)
async def send_tenant_email_test(
    tenant_id: UUID | None = None, current_user: UserRecord = Depends(get_current_admin)
) -> TenantEmailTestOut:
    try:
        await tenant_email_admin.test_config(current_user, tenant_id)
    except smtplib.SMTPException, OSError, ValueError, OutboundPolicyError:
        raise HTTPException(400, "SMTP test failed. Check the saved host, port, TLS mode and credentials.") from None
    return TenantEmailTestOut(message="Test email sent to your administrator email address")
