"""Organization-admin purchase workflow for extra accounts and agents (platform admins read any company).

The evidence upload and download live in ``capacity_evidence``."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.security import get_current_user
from app.records.user import UserRecord
from app.schemas.capacity import (
    CapacityOrderCreateRequest,
    CapacityOrderOut,
    CapacityOrderPage,
    CapacityOverviewOut,
)
from app.services.capacity_orders import (
    CapacityOrderView,
    CapacityOverview,
    cancel_order,
    create_order,
    get_order,
    list_company_orders,
    overview,
)

router = APIRouter(prefix="/capacity", tags=["capacity"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


def _order(view: CapacityOrderView) -> CapacityOrderOut:
    return CapacityOrderOut.from_record(view.order, view.tenant_name)


def _overview(data: CapacityOverview) -> CapacityOverviewOut:
    return CapacityOverviewOut(
        tenant_id=data.tenant_id,
        capped=data.capped,
        tier=data.tier,
        included_accounts=data.included_accounts,
        included_agents=data.included_agents,
        extra_accounts=data.extra_accounts,
        extra_agents=data.extra_agents,
        account_limit=data.account_limit,
        agent_limit=data.agent_limit,
        accounts_used=data.accounts_used,
        agents_used=data.agents_used,
        account_unit_usd=data.account_unit_usd,
        agent_unit_usd=data.agent_unit_usd,
        today=data.today,
        days_in_month=data.days_in_month,
        remaining_days=data.remaining_days,
        payments_configured=data.payments_configured,
        usd_vnd_rate=data.usd_vnd_rate,
        open_orders=[CapacityOrderOut.from_record(order) for order in data.open_orders],
        current_renewal=CapacityOrderOut.from_record(data.current_renewal)
        if data.current_renewal is not None
        else None,
    )


@router.get("/overview", response_model=CapacityOverviewOut)
async def get_capacity_overview(
    tenant_id: UUID | None = None, user: UserRecord = Depends(get_current_user)
) -> CapacityOverviewOut:
    return _overview(await overview(user.id, tenant_id))


@router.post("/orders", response_model=CapacityOrderOut, status_code=status.HTTP_201_CREATED)
async def create_capacity_order(
    data: CapacityOrderCreateRequest, request: Request, user: UserRecord = Depends(get_current_user)
) -> CapacityOrderOut:
    view = await create_order(user.id, data.extra_accounts, data.extra_agents, client_ip=_client_ip(request))
    return _order(view)


@router.get("/orders", response_model=CapacityOrderPage)
async def list_capacity_orders(
    tenant_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: UserRecord = Depends(get_current_user),
) -> CapacityOrderPage:
    views, has_more = await list_company_orders(user.id, tenant_id, limit=limit, offset=offset)
    return CapacityOrderPage(items=[_order(view) for view in views], has_more=has_more)


@router.get("/orders/{order_id}", response_model=CapacityOrderOut)
async def get_capacity_order(order_id: UUID, user: UserRecord = Depends(get_current_user)) -> CapacityOrderOut:
    return _order(await get_order(user.id, order_id))


@router.post("/orders/{order_id}/cancel", response_model=CapacityOrderOut)
async def cancel_capacity_order(
    order_id: UUID, request: Request, user: UserRecord = Depends(get_current_user)
) -> CapacityOrderOut:
    return _order(await cancel_order(user.id, order_id, client_ip=_client_ip(request)))
