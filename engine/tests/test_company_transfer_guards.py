import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api.company_admissions import router
from app.core.security import get_current_user
from app.records.company_admission import AdmissionMethod
from app.records.tenant import TenantRecord
from app.services.company_admissions import submit_request
from company_transfer_fixture import TransferFixture, transfer_fixture

__all__ = ["transfer_fixture"]


@pytest.mark.parametrize("method", list(AdmissionMethod))
async def test_default_member_with_assets_must_choose_disposition(
    transfer_fixture: TransferFixture, method: AdmissionMethod
) -> None:
    # Given an asset-owning default-org member entering through any admission method.
    fixture = transfer_fixture
    # When no explicit transfer choice has been recorded.
    with pytest.raises(HTTPException) as error:
        await submit_request(fixture.applicant, fixture.target.id, method)
    # Then the shared seam prevents join/domain/SSO bypasses before creating admission.
    assert error.value.status_code == 409
    assert error.value.detail == {"code": "transfer_choice_required"}
    assert not fixture.admissions.rows


@pytest.mark.parametrize("tenantless", [False, True])
async def test_assetless_member_can_request_admission_without_inventing_payment(
    transfer_fixture: TransferFixture, tenantless: bool
) -> None:
    # Given an assetless default-org or tenantless member.
    fixture = transfer_fixture
    fixture.owned_agents = False
    if tenantless:
        fixture.applicant.tenant_id = None
    # When a normal admission is requested.
    result = await submit_request(fixture.applicant, fixture.target.id, AdmissionMethod.SSO)
    # Then pending admission exists without a paid or consent request.
    assert result.id in fixture.admissions.rows
    assert not fixture.transfers.rows


@pytest.mark.parametrize("method", list(AdmissionMethod))
async def test_nondefault_source_is_forbidden_for_every_admission_method(
    transfer_fixture: TransferFixture, method: AdmissionMethod
) -> None:
    # Given an ordinary company member, even without assets.
    fixture = transfer_fixture
    source = TenantRecord(id=fixture.source.id, name="Existing company", slug="company")
    fixture.admissions.tenants[source.id] = source
    fixture.owned_agents = False
    # When an alternate method requests another company's admission.
    with pytest.raises(HTTPException) as error:
        await submit_request(fixture.applicant, fixture.target.id, method)
    # Then source policy rejects it server-side.
    assert error.value.status_code == 409
    assert not fixture.admissions.rows


@pytest.mark.parametrize("path", ["transfer-quote", "transfers/mine", "transfers/review"])
async def test_transfer_reads_require_member_authentication(path: str) -> None:
    # Given the actual public member router with no authenticated session.
    app = FastAPI()
    app.include_router(router, prefix="/api")
    # When a client requests the transfer contract.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://transfer.test") as client:
        response = await client.get(f"/api/company-admissions/{path}")
    # Then member authentication runs before any storage work.
    assert response.status_code == 401


async def test_quote_is_authenticated_member_endpoint_not_admin(transfer_fixture: TransferFixture) -> None:
    # Given an ordinary member and the real quote HTTP route.
    fixture = transfer_fixture
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: fixture.applicant
    # When the member requests their transfer quote.
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://transfer.test") as client:
            response = await client.get("/api/company-admissions/transfer-quote")
    finally:
        app.dependency_overrides.clear()
    # Then typed policy and personal eligibility are visible without admin access.
    assert response.status_code == 200
    assert response.json() == {
        "fee_amount_minor": 3700,
        "currency": "USD",
        "instructions": "Offline payment reference",
        "source_eligible": True,
        "has_owned_agents": True,
        "explicit_choice_required": True,
        "free_requires_destructive_consent": True,
    }
