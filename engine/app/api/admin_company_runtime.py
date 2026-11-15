"""Platform-admin runtime inventory, mounted without request-wide CRUD binding."""

from uuid import UUID

from fastapi import APIRouter, Depends

from app.core.security import require_role
from app.schemas.admin_companies import CompanyRuntimePageOut
from app.services.admin_companies import DEFAULT_PAGE_SIZE
from app.services.admin_company_runtime import company_runtime_page

router = APIRouter(prefix="/admin/companies", tags=["admin"], dependencies=[Depends(require_role("platform_admin"))])


@router.get("/{company_id}/runtime", response_model=CompanyRuntimePageOut)
async def get_company_runtime(
    company_id: UUID,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> CompanyRuntimePageOut:
    return await company_runtime_page(company_id, cursor, limit)
