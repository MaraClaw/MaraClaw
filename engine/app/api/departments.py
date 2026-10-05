"""Tenant-local department directory API."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.acl_locks import lock_tenants, lock_user
from app.core.security import get_current_user, raise_if_password_change_required
from app.dao.local_department_dao import local_department_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.errors import UniqueViolationError
from app.db.session import connection_ctx
from app.records.user import UserRecord
from app.schemas.department import AssignmentOut, DepartmentAssignment, DepartmentCreate, DepartmentOut, DepartmentsOut
from app.services.local_departments import admin_tenant, assign_department

router = APIRouter(tags=["departments"])


@router.get("/departments", response_model=DepartmentsOut)
async def list_departments(
    tenant_id: UUID | None = Query(None), current_user: UserRecord = Depends(get_current_user)
) -> DepartmentsOut:
    tenant = admin_tenant(current_user, tenant_id)
    if await tenant_dao.get(tenant) is None:
        raise HTTPException(404, "Organization not found")
    departments = await local_department_dao.list_for_tenant(tenant)
    return DepartmentsOut(
        departments=[DepartmentOut.model_validate(row) for row in departments],
        assignments=await local_department_dao.list_assignments(tenant),
    )


@router.post("/departments", response_model=DepartmentOut, status_code=201)
async def create_department(
    data: DepartmentCreate, tenant_id: UUID | None = Query(None), current_user: UserRecord = Depends(get_current_user)
) -> DepartmentOut:
    tenant = admin_tenant(current_user, tenant_id)
    try:
        async with connection_ctx() as db:
            await lock_tenants(db, (tenant, current_user.tenant_id))
            await lock_user(db, current_user.id, current_user.tenant_id)
            actor = await user_dao.get_with_identity(current_user.id, fresh=True)
            if actor is None or not actor.is_active:
                raise HTTPException(403, "Administrator is inactive")
            raise_if_password_change_required(actor)
            admin_tenant(actor, tenant)
            if await tenant_dao.get(tenant) is None:
                raise HTTPException(404, "Organization not found")
            row = await local_department_dao.create(obj_in={"tenant_id": tenant, "name": data.name})
            return DepartmentOut.model_validate(row)
    except UniqueViolationError as exc:
        raise HTTPException(409, "Department name already exists") from exc


@router.put("/users/{user_id}/department", response_model=AssignmentOut)
async def set_department(
    user_id: UUID,
    data: DepartmentAssignment,
    tenant_id: UUID | None = Query(None),
    current_user: UserRecord = Depends(get_current_user),
) -> AssignmentOut:
    tenant = admin_tenant(current_user, tenant_id)
    return await assign_department(
        current_user, AssignmentOut(user_id=user_id, tenant_id=tenant, department_id=data.department_id)
    )
