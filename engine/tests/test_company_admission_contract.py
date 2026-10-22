from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api.company_admissions import router
from app.core.security import get_current_user
from app.records.company_admission import AdmissionStatus, CompanyAdmissionRecord
from app.records.user import UserRecord
from app.schemas.company_admission import AdmissionDecision


def test_record_decodes_withdrawn_terminal_history():
    # Given a withdrawn PostgreSQL row with no reviewer identity.
    terminal = datetime(2026, 10, 21, tzinfo=UTC)
    row = {
        "id": uuid4(),
        "applicant_user_id": uuid4(),
        "target_tenant_id": uuid4(),
        "method": "invitation",
        "status": "withdrawn",
        "created_at": terminal,
        "decided_at": terminal,
        "decided_by_user_id": None,
    }

    # When the persistence boundary decodes the record.
    record = CompanyAdmissionRecord.from_row(row)

    # Then withdrawal remains distinct from a reviewer decision.
    assert record.status is AdmissionStatus.WITHDRAWN
    assert record.decided_at == terminal
    assert record.decided_by_user_id is None


def test_reviewer_cannot_parse_withdrawal_as_a_decision():
    # Given withdrawal submitted through the reviewer input contract.
    payload = {"decision": "withdrawn"}

    # When boundary validation parses the decision.
    with pytest.raises(ValidationError) as error:
        AdmissionDecision.model_validate(payload)

    # Then only approved/rejected remain valid reviewer operations.
    assert error.value.errors()[0]["type"] == "literal_error"


@pytest.mark.parametrize("path", ["withdraw", "decision"])
async def test_admission_mutations_require_authentication(path: str):
    # Given the actual admissions HTTP router without an authenticated session.
    app = FastAPI()
    app.include_router(router, prefix="/api")

    # When an anonymous client requests a terminal transition.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://admissions.test") as client:
        response = await client.post(f"/api/company-admissions/{uuid4()}/{path}", json={"decision": "approved"})

    # Then authentication fails before the database is accessed.
    assert response.status_code == 401


async def test_reviewer_withdrawal_payload_is_rejected_over_http():
    # Given an authenticated reviewer and the real request-validation boundary.
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: UserRecord(id=uuid4(), role="org_admin")

    # When withdrawal is sent as a reviewer decision.
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://admissions.test") as client:
            response = await client.post(f"/api/company-admissions/{uuid4()}/decision", json={"decision": "withdrawn"})
    finally:
        app.dependency_overrides.clear()

    # Then the route rejects it before calling the decision service.
    assert response.status_code == 422


async def test_review_page_requires_authentication():
    # Given the actual HTTP router without a session.
    app = FastAPI()
    app.include_router(router, prefix="/api")

    # When even a malformed continuation is submitted anonymously.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://admissions.test") as client:
        response = await client.get("/api/company-admissions/review/page", params={"cursor": "%%%"})

    # Then the cursor cannot substitute for authentication.
    assert response.status_code == 401


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"limit": "two"}, {"status": "unknown"}])
async def test_review_page_rejects_invalid_query_contract(params: dict[str, str | int]):
    # Given an authenticated HTTP boundary with invalid pagination inputs.
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: UserRecord(id=uuid4(), role="org_admin")

    # When framework query parsing runs before any database call.
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://admissions.test") as client:
            response = await client.get("/api/company-admissions/review/page", params=params)
    finally:
        app.dependency_overrides.clear()

    # Then invalid limits/status remain standard typed 422 responses.
    assert response.status_code == 422
