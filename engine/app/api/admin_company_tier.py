"""Platform-admin company tier preview and apply endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends

from app.core.security import get_current_user
from app.records.user import UserRecord
from app.schemas.admin_companies import (
    CompanyTierChangeOut,
    CompanyTierChangeRequest,
    CompanyTierPreview,
    CompanyTierPreviewRequest,
)
from app.services.organization_tier_changes import apply_tier_change, preview_tier_change

router = APIRouter(prefix="/admin/companies", tags=["admin"])


@router.post("/{tenant_id}/tier/preview", response_model=CompanyTierPreview)
async def preview_company_tier(
    tenant_id: UUID, data: CompanyTierPreviewRequest, user: UserRecord = Depends(get_current_user)
) -> CompanyTierPreview:
    return await preview_tier_change(user, tenant_id, data.target_tier)


@router.patch("/{tenant_id}/tier", response_model=CompanyTierChangeOut)
async def change_company_tier(
    tenant_id: UUID, data: CompanyTierChangeRequest, user: UserRecord = Depends(get_current_user)
) -> CompanyTierChangeOut:
    return await apply_tier_change(user, tenant_id, data)
