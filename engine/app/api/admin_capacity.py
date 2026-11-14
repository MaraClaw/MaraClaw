"""Platform-admin review of capacity orders, and the genesis-only payment settings."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.core.security import get_current_user
from app.records.capacity_order import CapacityOrderStatus
from app.records.user import UserRecord
from app.schemas.capacity import (
    CapacityDecisionRequest,
    CapacityOrderOut,
    CapacityOrderPage,
    CapacityPaymentSettingsOut,
    CapacityPaymentSettingsRequest,
)
from app.services.capacity_orders import decide, get_order, list_review_orders
from app.services.capacity_payment_settings import (
    PaymentSettings,
    read_settings_for_admin,
    update_payment_settings,
)

router = APIRouter(prefix="/admin", tags=["admin"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


def _settings_out(settings: PaymentSettings | None) -> CapacityPaymentSettingsOut:
    if settings is None:
        return CapacityPaymentSettingsOut(
            configured=False, bank_name=None, account_number=None, branch_name=None, usd_vnd_rate=None
        )
    return CapacityPaymentSettingsOut(
        configured=True,
        bank_name=settings.bank_name,
        account_number=settings.account_number,
        branch_name=settings.branch_name,
        usd_vnd_rate=settings.usd_vnd_rate,
    )


@router.get("/capacity-orders", response_model=CapacityOrderPage)
async def review_capacity_orders(
    status: CapacityOrderStatus = CapacityOrderStatus.SUBMITTED,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: UserRecord = Depends(get_current_user),
) -> CapacityOrderPage:
    views, has_more = await list_review_orders(user.id, status, limit=limit, offset=offset)
    return CapacityOrderPage(
        items=[CapacityOrderOut.from_record(v.order, v.tenant_name) for v in views], has_more=has_more
    )


@router.get("/capacity-orders/{order_id}", response_model=CapacityOrderOut)
async def get_review_capacity_order(order_id: UUID, user: UserRecord = Depends(get_current_user)) -> CapacityOrderOut:
    view = await get_order(user.id, order_id, platform_only=True)
    return CapacityOrderOut.from_record(view.order, view.tenant_name)


@router.post("/capacity-orders/{order_id}/decision", response_model=CapacityOrderOut)
async def decide_capacity_order(
    order_id: UUID, data: CapacityDecisionRequest, request: Request, user: UserRecord = Depends(get_current_user)
) -> CapacityOrderOut:
    view = await decide(user.id, order_id, data.action, data.note, client_ip=_client_ip(request))
    return CapacityOrderOut.from_record(view.order, view.tenant_name)


@router.get("/capacity-payment-settings", response_model=CapacityPaymentSettingsOut)
async def get_capacity_payment_settings(user: UserRecord = Depends(get_current_user)) -> CapacityPaymentSettingsOut:
    return _settings_out(await read_settings_for_admin(user.id))


@router.put("/capacity-payment-settings", response_model=CapacityPaymentSettingsOut)
async def put_capacity_payment_settings(
    data: CapacityPaymentSettingsRequest, request: Request, user: UserRecord = Depends(get_current_user)
) -> CapacityPaymentSettingsOut:
    settings = PaymentSettings(data.bank_name, data.account_number, data.branch_name, data.usd_vnd_rate)
    return _settings_out(await update_payment_settings(user.id, settings, client_ip=_client_ip(request)))
