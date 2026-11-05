from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidTag
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api import organization_admissions as api
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.records.organization_plan import OrganizationPlanId
from app.records.user import UserRecord
from app.schemas.organization_admission import OrganizationAdmissionDecisionRequest, OrganizationAdmissionRequest
from app.services.organization_admission_credentials import seal, unseal

CATALOG = {
    "plans": [
        {
            "id": "pro",
            "name": "Pro",
            "currency": "USD",
            "monthly_price_usd": 99,
            "included_accounts": 5,
            "additional_account_monthly_usd": 12,
            "max_accounts": 25,
            "max_agents_per_account": 2,
            "storage_limit_bytes": 1073741824,
            "max_file_size_bytes": 26214400,
        },
        {
            "id": "max",
            "name": "Max",
            "currency": "USD",
            "monthly_price_usd": 299,
            "included_accounts": 15,
            "additional_account_monthly_usd": 10,
            "max_accounts": 75,
            "max_agents_per_account": 5,
            "storage_limit_bytes": 5368709120,
            "max_file_size_bytes": 52428800,
        },
        {
            "id": "ultra",
            "name": "Ultra",
            "currency": "USD",
            "monthly_price_usd": 799,
            "included_accounts": 40,
            "additional_account_monthly_usd": 8,
            "max_accounts": 200,
            "max_agents_per_account": 10,
            "storage_limit_bytes": 10737418240,
            "max_file_size_bytes": 104857600,
        },
    ]
}


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://organization.test") as http:
        yield http, app


async def test_catalog_is_public_and_exact(client: tuple[AsyncClient, FastAPI]):
    http, _ = client
    response = await http.get("/api/organization-plans")
    assert (response.status_code, response.json()) == (200, CATALOG)


async def test_submission_passes_normalized_email_and_tier_and_returns_generic_ack(
    client: tuple[AsyncClient, FastAPI], monkeypatch: pytest.MonkeyPatch
):
    http, _ = client
    submit = AsyncMock()
    monkeypatch.setattr(api, "submit_request", submit)
    response = await http.post(
        "/api/organization-admissions",
        json={"organization_name": "  Ops  Co. ", "email": "  Ops@Example.COM ", "tier": "ultra"},
    )
    assert (response.status_code, response.json()) == (202, {"status": "pending"})
    submit.assert_awaited_once_with("ops@example.com", "Ops  Co.", OrganizationPlanId.ULTRA)


@pytest.mark.parametrize(
    "payload",
    [
        {"organization_name": "Acme", "email": "not-an-email", "tier": "pro"},
        {"organization_name": "Acme", "email": "", "tier": "pro"},
        {"organization_name": "Acme", "email": "a@example.com", "tier": "Pro"},
        {"organization_name": "Acme", "email": "a@example.com", "tier": "enterprise"},
        {"organization_name": "Acme", "email": "a@example.com"},
        {"organization_name": "Acme", "email": "a@example.com", "tier": "pro", "status": "approved"},
        {"email": "a@example.com", "tier": "pro"},
        {"organization_name": "", "email": "a@example.com", "tier": "pro"},
        {"organization_name": "   ", "email": "a@example.com", "tier": "pro"},
        {"organization_name": "x" * 201, "email": "a@example.com", "tier": "pro"},
        {"organization_name": "Acme\nBcc: x@example.com", "email": "a@example.com", "tier": "pro"},
    ],
)
async def test_invalid_submission_is_rejected_before_the_service(
    client: tuple[AsyncClient, FastAPI], monkeypatch: pytest.MonkeyPatch, payload: dict[str, str]
):
    http, _ = client
    submit = AsyncMock()
    monkeypatch.setattr(api, "submit_request", submit)
    assert (await http.post("/api/organization-admissions", json=payload)).status_code == 422
    submit.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "path"), [("get", ""), ("post", f"/{uuid4()}/decision"), ("post", f"/{uuid4()}/retry-email")]
)
async def test_review_routes_require_authentication(client: tuple[AsyncClient, FastAPI], method: str, path: str):
    http, _ = client
    response = await http.request(method, f"/api/admin/organization-admissions{path}", json={"decision": "approve"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "withdrawn"}, {"status": "unknown"}]
)
async def test_review_query_contract_is_typed(client: tuple[AsyncClient, FastAPI], params: dict[str, str | int]):
    http, app = client
    app.dependency_overrides[get_current_user] = lambda: UserRecord(id=uuid4(), role=UserRole.PLATFORM_ADMIN)
    try:
        response = await http.get("/api/admin/organization-admissions", params=params)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 422


@pytest.mark.parametrize("decision", ["approved", "withdrawn", "", None])
def test_decision_command_is_approve_or_reject_only(decision: str | None):
    with pytest.raises(ValidationError):
        _ = OrganizationAdmissionDecisionRequest.model_validate({"decision": decision})


def test_decision_note_is_trimmed_bounded_and_blank_is_absent():
    assert (
        OrganizationAdmissionDecisionRequest.model_validate({"decision": "approve", "note": f" {'x' * 1000} "}).note
        == "x" * 1000
    )
    assert OrganizationAdmissionDecisionRequest.model_validate({"decision": "reject", "note": "  "}).note is None
    with pytest.raises(ValidationError):
        _ = OrganizationAdmissionDecisionRequest.model_validate({"decision": "reject", "note": "x" * 1001})


def test_request_email_is_lowercased_and_trimmed():
    assert (
        OrganizationAdmissionRequest.model_validate(
            {"organization_name": "Acme", "email": " A.B@Example.Com\n", "tier": "pro"}
        ).email
        == "a.b@example.com"
    )


def test_credential_payload_is_bound_to_its_request():
    request_id, other = uuid4(), uuid4()
    sealed = seal(request_id, "temporary-secret")
    assert unseal(request_id, sealed) == "temporary-secret"
    with pytest.raises(InvalidTag):
        _ = unseal(other, sealed)
    with pytest.raises(InvalidTag):
        _ = unseal(request_id, sealed[:-1] + bytes([sealed[-1] ^ 1]))
