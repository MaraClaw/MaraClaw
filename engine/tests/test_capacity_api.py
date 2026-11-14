"""Capacity order HTTP contract and authorization matrix with fakes (real behavior: test_capacity_orders_postgres)."""

from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import iter_route_contexts
from httpx import ASGITransport, AsyncClient

from app import main
from app.api import admin_capacity, capacity, capacity_evidence, enterprise
from app.core.domain.approvals import ApprovalAction
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.records.capacity_order import (
    CapacityEvidence,
    CapacityOrderKind,
    CapacityOrderRecord,
    CapacityOrderStatus,
    CapacityRecipient,
)
from app.records.identity import IdentityRecord
from app.records.organization_plan import OrganizationPlanId
from app.records.user import UserRecord
from app.schemas.capacity import CapacityEvidenceOut, CapacityOrderOut
from app.services import capacity_orders, capacity_payment_settings
from app.services.capacity_evidence import MAX_EVIDENCE_BYTES
from app.services.capacity_orders import EvidenceFile
from app.services.capacity_payment_settings import RESERVED_SETTING_KEYS, SETTINGS_KEY, PaymentSettings

TENANT_A, TENANT_B = UUID(int=1), UUID(int=2)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def person(role: str, tenant: UUID | None = TENANT_A, *, flag: bool = False, genesis: bool = False) -> UserRecord:
    identity = IdentityRecord(id=uuid4(), email=f"{role}@x.test", is_platform_admin=flag)
    return UserRecord(id=uuid4(), tenant_id=tenant, role=UserRole(role), is_genesis=genesis, identity=identity)


def order_of(tenant: UUID, *, status: CapacityOrderStatus = CapacityOrderStatus.SUBMITTED) -> CapacityOrderRecord:
    return CapacityOrderRecord(
        id=uuid4(),
        tenant_id=tenant,
        reference="MCABCDEFGH",
        kind=CapacityOrderKind.PURCHASE,
        status=status,
        tier=OrganizationPlanId.PRO,
        billing_month=date(2027, 1, 1),
        extra_accounts=1,
        extra_agents=0,
        account_unit_usd=5,
        agent_unit_usd=35,
        days_charged=31,
        days_in_month=31,
        amount_usd_cents=500,
        usd_vnd_rate=25_000,
        amount_vnd=125_000,
        recipient=CapacityRecipient("ACB", "123", "HCM"),
        created_at=datetime(2027, 1, 1, tzinfo=UTC),
        evidence=CapacityEvidence("_capacity_payments/k.png", "image/png", "proof.png", 40, "ab" * 32),
    )


class FakeOrders:
    def __init__(self, *rows: CapacityOrderRecord) -> None:
        self.rows: dict[UUID, CapacityOrderRecord] = {row.id: row for row in rows}
        self.listed: list[str] = []

    async def get(self, order_id: UUID) -> CapacityOrderRecord | None:
        return self.rows.get(order_id)

    get_locked = get

    async def list_for_tenant(self, tenant_id: UUID, *, limit: int, offset: int) -> list[CapacityOrderRecord]:
        del limit, offset
        self.listed.append(f"tenant:{tenant_id}")
        return [row for row in self.rows.values() if row.tenant_id == tenant_id]

    async def list_by_status(
        self, status: CapacityOrderStatus | None, *, limit: int, offset: int
    ) -> list[CapacityOrderRecord]:
        del limit, offset
        self.listed.append(f"status:{status}")
        return list(self.rows.values())


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeOrders]:
    """Install a signed-in principal and an order table; returns the fake order table."""

    def install(user: UserRecord, *rows: CapacityOrderRecord) -> FakeOrders:
        table = FakeOrders(*rows)

        @asynccontextmanager
        async def connection() -> AsyncGenerator[None]:
            yield

        async def principal(user_id: UUID, *, fresh: bool = False) -> UserRecord:
            assert fresh
            assert user_id == user.id
            return user

        async def names(ids: list[UUID]) -> list[SimpleNamespace]:
            return [SimpleNamespace(id=tenant_id, name=f"Company {tenant_id.int}") for tenant_id in ids]

        monkeypatch.setattr(capacity_orders, "connection_ctx", connection)
        monkeypatch.setattr(capacity_orders, "load_user_principal", principal)
        monkeypatch.setattr(capacity_orders, "orders", table)
        monkeypatch.setattr(capacity_orders, "tenant_dao", SimpleNamespace(get_many=names))
        return table

    return install


def status_of(error: pytest.ExceptionInfo[HTTPException]) -> int:
    return error.value.status_code


# ── authorization matrix ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("user", "outcome"),
    [
        (person("org_admin", TENANT_A), 200),
        (person("org_admin", TENANT_B), 404),
        (person("member", TENANT_A), 403),
        (person("agent_admin", TENANT_A), 403),
        (person("platform_admin", None), 200),
        (person("member", TENANT_B, flag=True), 200),
    ],
)
async def test_an_order_is_readable_by_its_companys_org_admins_and_platform_admins(
    world: Callable[..., FakeOrders], user: UserRecord, outcome: int
):
    order = order_of(TENANT_A)
    _ = world(user, order)

    if outcome == 200:
        view = await capacity_orders.get_order(user.id, order.id)
        assert view.order.id == order.id
        assert view.tenant_name == f"Company {TENANT_A.int}"
    else:
        with pytest.raises(HTTPException) as refused:
            _ = await capacity_orders.get_order(user.id, order.id)
        assert status_of(refused) == outcome


async def test_the_review_view_of_one_order_is_for_platform_admins_only(world: Callable[..., FakeOrders]):
    order = order_of(TENANT_A)
    admin = person("org_admin", TENANT_A)
    _ = world(admin, order)
    with pytest.raises(HTTPException) as refused:
        _ = await capacity_orders.get_order(admin.id, order.id, platform_only=True)
    assert status_of(refused) == 403

    reviewer = person("platform_admin", None)
    _ = world(reviewer, order)
    assert (await capacity_orders.get_order(reviewer.id, order.id, platform_only=True)).order.id == order.id


async def test_the_review_queue_is_for_platform_admins_only(world: Callable[..., FakeOrders]):
    for user in (person("org_admin", TENANT_A), person("member", TENANT_A), person("agent_admin", TENANT_A)):
        _ = world(user, order_of(TENANT_A))
        with pytest.raises(HTTPException) as refused:
            _ = await capacity_orders.list_review_orders(user.id, CapacityOrderStatus.SUBMITTED, limit=10, offset=0)
        assert status_of(refused) == 403

    reviewer = person("platform_admin", None, genesis=False)
    _ = world(reviewer, order_of(TENANT_A), order_of(TENANT_B))
    views, has_more = await capacity_orders.list_review_orders(
        reviewer.id, CapacityOrderStatus.SUBMITTED, limit=10, offset=0
    )
    assert (len(views), has_more) == (2, False)
    assert {view.tenant_name for view in views} == {f"Company {TENANT_A.int}", f"Company {TENANT_B.int}"}


async def test_the_company_list_is_own_company_for_org_admins_and_named_for_platform_admins(
    world: Callable[..., FakeOrders],
):
    admin = person("org_admin", TENANT_A)
    table = world(admin, order_of(TENANT_A), order_of(TENANT_B))
    views, _ = await capacity_orders.list_company_orders(admin.id, None, limit=10, offset=0)
    assert [view.order.tenant_id for view in views] == [TENANT_A]
    assert table.listed == [f"tenant:{TENANT_A}"]
    with pytest.raises(HTTPException) as foreign:
        _ = await capacity_orders.list_company_orders(admin.id, TENANT_B, limit=10, offset=0)
    assert status_of(foreign) == 403

    reviewer = person("platform_admin", None)
    _ = world(reviewer, order_of(TENANT_B))
    with pytest.raises(HTTPException) as unnamed:
        _ = await capacity_orders.list_company_orders(reviewer.id, None, limit=10, offset=0)
    assert status_of(unnamed) == 400
    named, _ = await capacity_orders.list_company_orders(reviewer.id, TENANT_B, limit=10, offset=0)
    assert [view.order.tenant_id for view in named] == [TENANT_B]

    member = person("member", TENANT_A)
    _ = world(member)
    with pytest.raises(HTTPException) as refused:
        _ = await capacity_orders.list_company_orders(member.id, None, limit=10, offset=0)
    assert status_of(refused) == 403


@pytest.mark.parametrize(
    "user",
    [person("platform_admin", None), person("member", TENANT_A), person("agent_admin", TENANT_A)],
    ids=["platform-admin", "member", "agent-admin"],
)
async def test_only_an_org_admin_buys_pays_or_cancels(world: Callable[..., FakeOrders], user: UserRecord):
    order = order_of(TENANT_A, status=CapacityOrderStatus.AWAITING_PAYMENT)
    _ = world(user, order)

    with pytest.raises(HTTPException) as bought:
        _ = await capacity_orders.create_order(user.id, 1, 0)
    with pytest.raises(HTTPException) as paid:
        _ = await capacity_orders.submit_evidence(user.id, order.id, data=PNG, filename="proof.png")
    with pytest.raises(HTTPException) as cancelled:
        _ = await capacity_orders.cancel_order(user.id, order.id)

    assert (status_of(bought), status_of(paid), status_of(cancelled)) == (403, 403, 403)


async def test_another_companys_org_admin_cannot_see_pay_or_cancel_the_order(world: Callable[..., FakeOrders]):
    order = order_of(TENANT_A, status=CapacityOrderStatus.AWAITING_PAYMENT)
    stranger = person("org_admin", TENANT_B)
    _ = world(stranger, order)

    with pytest.raises(HTTPException) as paid:
        _ = await capacity_orders.submit_evidence(stranger.id, order.id, data=PNG, filename="proof.png")
    with pytest.raises(HTTPException) as cancelled:
        _ = await capacity_orders.cancel_order(stranger.id, order.id)

    assert (status_of(paid), status_of(cancelled)) == (404, 404)


@pytest.mark.parametrize(
    "user",
    [person("org_admin", TENANT_A), person("member", TENANT_A), person("agent_admin", TENANT_B)],
    ids=["org-admin", "member", "agent-admin"],
)
async def test_only_platform_admins_decide(world: Callable[..., FakeOrders], user: UserRecord):
    order = order_of(TENANT_A)
    _ = world(user, order)

    with pytest.raises(HTTPException) as refused:
        _ = await capacity_orders.decide(user.id, order.id, ApprovalAction.APPROVE, None)

    assert status_of(refused) == 403


async def test_evidence_is_readable_by_its_org_admin_and_platform_admins_only(
    world: Callable[..., FakeOrders], monkeypatch: pytest.MonkeyPatch
):
    order = order_of(TENANT_A)
    stored = AsyncMock(return_value=PNG)
    monkeypatch.setattr(capacity_orders, "get_storage_backend", lambda: SimpleNamespace(read_bytes_bounded=stored))
    for user, outcome in (
        (person("org_admin", TENANT_A), 200),
        (person("platform_admin", None), 200),
        (person("org_admin", TENANT_B), 404),
        (person("member", TENANT_A), 403),
    ):
        _ = world(user, order)
        if outcome == 200:
            file = await capacity_orders.read_evidence(user.id, order.id)
            assert (file.data, file.content_type, file.filename) == (PNG, "image/png", "proof.png")
        else:
            with pytest.raises(HTTPException) as refused:
                _ = await capacity_orders.read_evidence(user.id, order.id)
            assert status_of(refused) == outcome
    stored.assert_awaited_with("_capacity_payments/k.png", MAX_EVIDENCE_BYTES)


async def test_an_order_without_evidence_has_nothing_to_download(world: Callable[..., FakeOrders]):
    bare = replace(order_of(TENANT_A, status=CapacityOrderStatus.AWAITING_PAYMENT), evidence=None)
    reviewer = person("platform_admin", None)
    _ = world(reviewer, bare)

    with pytest.raises(HTTPException) as missing:
        _ = await capacity_orders.read_evidence(reviewer.id, bare.id)

    assert status_of(missing) == 404


async def test_a_file_that_is_not_a_png_jpeg_webp_or_pdf_is_refused_before_anything_is_stored(
    world: Callable[..., FakeOrders], monkeypatch: pytest.MonkeyPatch
):
    admin = person("org_admin", TENANT_A)
    _ = world(admin, order_of(TENANT_A, status=CapacityOrderStatus.AWAITING_PAYMENT))
    storage = AsyncMock()
    monkeypatch.setattr(capacity_orders, "get_storage_backend", storage)

    with pytest.raises(HTTPException) as html:
        _ = await capacity_orders.submit_evidence(admin.id, uuid4(), data=b"<html></html>", filename="proof.png")
    with pytest.raises(HTTPException) as huge:
        _ = await capacity_orders.submit_evidence(
            admin.id, uuid4(), data=PNG + b"\x00" * MAX_EVIDENCE_BYTES, filename="proof.png"
        )

    assert (status_of(html), status_of(huge)) == (415, 413)
    storage.assert_not_called()


# ── genesis-only settings, reserved key ─────────────────────────────────


@pytest.mark.parametrize(
    ("user", "allowed"),
    [
        (person("platform_admin", None, genesis=True), True),
        (person("platform_admin", None, genesis=False), False),
        (person("org_admin", TENANT_A, genesis=True), False),
        (person("member", TENANT_A), False),
    ],
    ids=["genesis-pa", "additional-pa", "genesis-org-admin", "member"],
)
async def test_payment_settings_are_for_the_genesis_platform_admin_only(
    monkeypatch: pytest.MonkeyPatch, user: UserRecord, allowed: bool
):
    async def principal(user_id: UUID, *, fresh: bool = False) -> UserRecord:
        assert fresh
        assert user_id == user.id
        return user

    monkeypatch.setattr(capacity_payment_settings, "load_user_principal", principal)

    if allowed:
        assert (await capacity_payment_settings.require_genesis_platform_admin(user.id)).id == user.id
    else:
        with pytest.raises(HTTPException) as refused:
            _ = await capacity_payment_settings.require_genesis_platform_admin(user.id)
        assert status_of(refused) == 403


def test_the_payment_settings_key_is_blocked_from_the_generic_endpoints():
    assert SETTINGS_KEY in RESERVED_SETTING_KEYS
    assert "system_email_platform" in RESERVED_SETTING_KEYS


async def test_the_generic_system_settings_endpoints_never_serve_or_write_the_payment_key():
    user = person("org_admin", TENANT_A, genesis=True)
    with pytest.raises(HTTPException) as read:
        _ = await enterprise.get_system_setting(SETTINGS_KEY, current_user=user)
    with pytest.raises(HTTPException) as write:
        _ = await enterprise.update_system_setting(SETTINGS_KEY, enterprise.SettingUpdate(value={"x": 1}), user)
    with pytest.raises(HTTPException) as internal:
        _ = await enterprise._update_system_setting(SETTINGS_KEY, enterprise.SettingUpdate(value={"x": 1}), user)
    assert (status_of(read), status_of(write), status_of(internal)) == (404, 404, 404)


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "text",
        {},
        {"bank_name": "ACB", "account_number": "1", "branch_name": "HCM"},
        {"bank_name": "ACB", "account_number": "1", "branch_name": "HCM", "usd_vnd_rate": 0},
        {"bank_name": "ACB", "account_number": "1", "branch_name": "HCM", "usd_vnd_rate": True},
        {"bank_name": "ACB", "account_number": "1", "branch_name": "HCM", "usd_vnd_rate": 25000.5},
        {"bank_name": "ACB", "account_number": "1", "branch_name": "HCM", "usd_vnd_rate": "25000"},
        {"bank_name": "ACB", "account_number": "1", "branch_name": "HCM", "usd_vnd_rate": 1_000_001},
        {"bank_name": "  ", "account_number": "1", "branch_name": "HCM", "usd_vnd_rate": 25000},
        {"bank_name": "ACB", "account_number": 1, "branch_name": "HCM", "usd_vnd_rate": 25000},
    ],
)
async def test_missing_or_malformed_stored_settings_mean_payments_are_not_configured(
    monkeypatch: pytest.MonkeyPatch, stored: object
):
    row = None if stored is None else SimpleNamespace(value=stored)
    monkeypatch.setattr(
        capacity_payment_settings, "system_setting_dao", SimpleNamespace(get_by_key=AsyncMock(return_value=row))
    )

    assert await capacity_payment_settings.load_payment_settings() is None


async def test_complete_stored_settings_load(monkeypatch: pytest.MonkeyPatch):
    value = {"bank_name": "ACB", "account_number": "0123", "branch_name": "HCM", "usd_vnd_rate": 25_400}
    monkeypatch.setattr(
        capacity_payment_settings,
        "system_setting_dao",
        SimpleNamespace(get_by_key=AsyncMock(return_value=SimpleNamespace(value=value))),
    )

    settings = await capacity_payment_settings.load_payment_settings()

    assert settings == PaymentSettings("ACB", "0123", "HCM", 25_400)
    assert settings is not None
    assert settings.recipient == CapacityRecipient("ACB", "0123", "HCM")


# ── HTTP contract ───────────────────────────────────────────────────────


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[AsyncClient, FastAPI, UserRecord]]:
    """The real routers with a signed-in fake user and every service call replaced."""
    app = FastAPI()
    app.include_router(capacity.router, prefix="/api")
    app.include_router(capacity_evidence.router, prefix="/api")
    app.include_router(admin_capacity.router, prefix="/api")
    user = person("org_admin", TENANT_A)
    app.dependency_overrides[get_current_user] = lambda: user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://capacity.test") as http:
        try:
            yield http, app, user
        finally:
            app.dependency_overrides.clear()


ROUTES = [
    ("GET", "/api/capacity/overview"),
    ("POST", "/api/capacity/orders"),
    ("GET", "/api/capacity/orders"),
    ("GET", f"/api/capacity/orders/{UUID(int=9)}"),
    ("POST", f"/api/capacity/orders/{UUID(int=9)}/evidence"),
    ("POST", f"/api/capacity/orders/{UUID(int=9)}/cancel"),
    ("GET", f"/api/capacity/orders/{UUID(int=9)}/evidence"),
    ("GET", "/api/admin/capacity-orders"),
    ("GET", f"/api/admin/capacity-orders/{UUID(int=9)}"),
    ("POST", f"/api/admin/capacity-orders/{UUID(int=9)}/decision"),
    ("GET", "/api/admin/capacity-payment-settings"),
    ("PUT", "/api/admin/capacity-payment-settings"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_every_route_of_the_real_application_needs_a_bearer_token(method: str, path: str):
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://capacity.test") as http:
        response = await http.request(method, path)

    assert response.status_code in (401, 403)


def test_every_route_is_mounted_in_the_application():
    mounted = {
        (method, context.path_format)
        for context in iter_route_contexts(main.app.routes)
        for method in context.methods or ()
    }
    for method, path in ROUTES:
        template = path.replace(str(UUID(int=9)), "{order_id}")
        assert (method, template) in mounted, (method, template)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"extra_accounts": 0, "extra_agents": 0},
        {"extra_accounts": -1, "extra_agents": 2},
        {"extra_accounts": 10_001, "extra_agents": 0},
        {"extra_accounts": 1, "extra_agents": 10_001},
        {"extra_accounts": 1.5},
        {"extra_accounts": "2"},
        {"extra_accounts": True},
        {"extra_accounts": 1, "price": 1},
    ],
)
async def test_an_order_request_needs_whole_nonnegative_quantities_with_a_positive_sum(
    client: tuple[AsyncClient, FastAPI, UserRecord], body: dict[str, object]
):
    http, _, _ = client
    assert (await http.post("/api/capacity/orders", json=body)).status_code == 422


async def test_a_valid_order_request_reaches_the_service(
    client: tuple[AsyncClient, FastAPI, UserRecord], monkeypatch: pytest.MonkeyPatch
):
    http, _, user = client
    created = capacity_orders.CapacityOrderView(order_of(TENANT_A, status=CapacityOrderStatus.AWAITING_PAYMENT), "Acme")
    service = AsyncMock(return_value=created)
    monkeypatch.setattr(capacity, "create_order", service)

    response = await http.post("/api/capacity/orders", json={"extra_accounts": 10_000, "extra_agents": 0})

    assert response.status_code == 201
    service.assert_awaited_once_with(user.id, 10_000, 0, client_ip="127.0.0.1")
    body = response.json()
    assert body["reference"] == "MCABCDEFGH"
    assert body["tenant_name"] == "Acme"
    assert body["recipient"] == {"bank_name": "ACB", "account_number": "123", "branch_name": "HCM"}
    assert "storage_key" not in str(body)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"action": "approve", "extra": 1},
        {"action": "maybe"},
        {"action": "reject"},
        {"action": "reject", "note": "   "},
        {"action": "reject", "note": "x" * 1001},
        {"action": "approve", "note": "x" * 1001},
    ],
)
async def test_a_decision_needs_a_known_action_and_a_note_to_reject(
    client: tuple[AsyncClient, FastAPI, UserRecord], body: dict[str, object]
):
    http, _, _ = client
    assert (await http.post(f"/api/admin/capacity-orders/{uuid4()}/decision", json=body)).status_code == 422


async def test_an_approval_needs_no_note_and_a_blank_one_is_dropped(
    client: tuple[AsyncClient, FastAPI, UserRecord], monkeypatch: pytest.MonkeyPatch
):
    http, _, user = client
    decided = capacity_orders.CapacityOrderView(order_of(TENANT_A, status=CapacityOrderStatus.APPROVED), None)
    service = AsyncMock(return_value=decided)
    monkeypatch.setattr(admin_capacity, "decide", service)
    order_id = uuid4()

    response = await http.post(
        f"/api/admin/capacity-orders/{order_id}/decision", json={"action": "approve", "note": " "}
    )

    assert response.status_code == 200
    service.assert_awaited_once_with(user.id, order_id, ApprovalAction.APPROVE, None, client_ip="127.0.0.1")


VALID_SETTINGS = {"bank_name": "ACB", "account_number": "0123 456", "branch_name": "HCM", "usd_vnd_rate": 25_400}


@pytest.mark.parametrize(
    "override",
    [
        {"bank_name": ""},
        {"bank_name": "   "},
        {"bank_name": "x" * 121},
        {"account_number": "12\n34"},
        {"branch_name": "tab\there"},
        {"branch_name": "bell\x07"},
        {"usd_vnd_rate": 0},
        {"usd_vnd_rate": 1_000_001},
        {"usd_vnd_rate": 25_400.5},
        {"usd_vnd_rate": "25400"},
        {"usd_vnd_rate": True},
        {"usd_vnd_rate": None},
        {"swift": "ABCDVNVX"},
    ],
)
async def test_payment_settings_are_validated_at_the_boundary(
    client: tuple[AsyncClient, FastAPI, UserRecord], override: dict[str, object]
):
    http, _, _ = client
    assert (
        await http.put("/api/admin/capacity-payment-settings", json={**VALID_SETTINGS, **override})
    ).status_code == 422


async def test_payment_settings_are_trimmed_and_saved_through_the_service(
    client: tuple[AsyncClient, FastAPI, UserRecord], monkeypatch: pytest.MonkeyPatch
):
    http, _, user = client
    service = AsyncMock(side_effect=lambda user_id, settings, client_ip=None: settings)
    monkeypatch.setattr(admin_capacity, "update_payment_settings", service)

    response = await http.put(
        "/api/admin/capacity-payment-settings", json={**VALID_SETTINGS, "bank_name": "  Vietcombank  "}
    )

    assert response.status_code == 200
    assert response.json() == {**VALID_SETTINGS, "bank_name": "Vietcombank", "configured": True}
    assert service.await_args is not None
    assert service.await_args.args[0] == user.id
    assert service.await_args.args[1].bank_name == "Vietcombank"


async def test_unset_payment_settings_read_as_not_configured(
    client: tuple[AsyncClient, FastAPI, UserRecord], monkeypatch: pytest.MonkeyPatch
):
    http, _, _ = client
    monkeypatch.setattr(admin_capacity, "read_settings_for_admin", AsyncMock(return_value=None))

    response = await http.get("/api/admin/capacity-payment-settings")

    assert response.json() == {
        "configured": False,
        "bank_name": None,
        "account_number": None,
        "branch_name": None,
        "usd_vnd_rate": None,
    }


async def test_the_evidence_upload_is_bounded_and_forwards_the_bytes_not_the_claimed_type(
    client: tuple[AsyncClient, FastAPI, UserRecord], monkeypatch: pytest.MonkeyPatch
):
    http, _, user = client
    submitted = capacity_orders.CapacityOrderView(order_of(TENANT_A), "Acme")
    service = AsyncMock(return_value=submitted)
    monkeypatch.setattr(capacity_evidence, "submit_evidence", service)
    order_id = uuid4()

    ok = await http.post(
        f"/api/capacity/orders/{order_id}/evidence", files={"file": ("../proof.png", PNG, "application/x-evil")}
    )
    huge = await http.post(
        f"/api/capacity/orders/{order_id}/evidence",
        files={"file": ("big.png", b"\x00" * (MAX_EVIDENCE_BYTES + 1), "image/png")},
    )
    missing = await http.post(f"/api/capacity/orders/{order_id}/evidence")

    assert ok.status_code == 200
    service.assert_awaited_once_with(user.id, order_id, data=PNG, filename="../proof.png", client_ip="127.0.0.1")
    assert huge.status_code == 413
    assert missing.status_code == 422


async def test_the_evidence_download_is_sandboxed_and_never_sniffed(
    client: tuple[AsyncClient, FastAPI, UserRecord], monkeypatch: pytest.MonkeyPatch
):
    http, _, user = client
    service = AsyncMock(return_value=EvidenceFile(PNG, "image/png", 'pr"oof\r\nx.png'))
    monkeypatch.setattr(capacity_evidence, "read_evidence", service)
    order_id = uuid4()

    response = await http.get(f"/api/capacity/orders/{order_id}/evidence")

    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["cache-control"] == "private, no-store"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("inline; filename=")
    assert "\r" not in disposition
    service.assert_awaited_once_with(user.id, order_id)


async def test_a_conflict_carries_a_machine_readable_code(client: tuple[AsyncClient, FastAPI, UserRecord], monkeypatch):
    http, _, _ = client
    monkeypatch.setattr(
        capacity,
        "create_order",
        AsyncMock(side_effect=capacity_orders.conflict("purchase_already_open", "Finish it first", order_id="o-1")),
    )

    response = await http.post("/api/capacity/orders", json={"extra_accounts": 1})

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "purchase_already_open",
        "message": "Finish it first",
        "order_id": "o-1",
    }


def test_the_order_schemas_never_name_the_storage_key():
    assert "storage_key" not in CapacityEvidenceOut.model_fields
    assert not any("storage_key" in name for name in CapacityOrderOut.model_fields)
