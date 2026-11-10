from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from json import dumps
from typing import Final, Literal, assert_never
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pydantic import TypeAdapter

from app.api import company_transfers
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.db.connection import DbConnection
from app.records.company_admission import AdmissionMethod
from app.records.company_transfer import CompanyTransferRecord, TransferChoice, TransferStatus
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.schemas.company_transfer import TransferReviewPage, TransferSummary
from app.services import company_transfer_review as review
from app.services.company_admissions import fresh_user, user_dao
from company_transfer_fixture import TransferFixture, transfer_fixture

__all__ = ["transfer_fixture"]

BASE: Final = "/api/company-admissions/transfers"
REQUEST_ID: Final = UUID(int=100)
type Surface = tuple[str, str, Literal["approved", "rejected"] | None]
SURFACES: Final[tuple[Surface, ...]] = (
    ("GET", "review", None),
    ("GET", "review/page", None),
    ("GET", str(REQUEST_ID), None),
    ("POST", f"{REQUEST_ID}/decision", "approved"),
    ("POST", f"{REQUEST_ID}/decision", "rejected"),
)


@pytest.fixture
async def client(
    transfer_fixture: TransferFixture,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, str], None],
) -> AsyncIterator[AsyncClient]:
    fixture = transfer_fixture
    fixture.transfers.rows[REQUEST_ID] = CompanyTransferRecord(
        id=REQUEST_ID,
        applicant_user_id=fixture.applicant.id,
        source_tenant_id=fixture.source.id,
        target_tenant_id=fixture.target.id,
        choice=TransferChoice.PAID,
        method=AdmissionMethod.VERIFIED_DOMAIN,
        fee_amount_minor=3700,
        currency="USD",
        instructions="Offline payment reference",
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
    )

    async def get_user(user_id: UUID, *, fresh: bool = False) -> UserRecord | None:
        if not fresh and user_id == fixture.reviewer.id:
            return fixture.reviewer
        return fixture.users.get(user_id)

    async def capture(response: Response) -> None:
        await response.aread()
        record_property(
            "http",
            dumps(
                {
                    "method": response.request.method,
                    "path": response.request.url.path,
                    "request": response.request.content.decode(),
                    "status": response.status_code,
                    "response": response.text,
                }
            ),
        )

    monkeypatch.setattr(user_dao, "get_with_identity", get_user)
    monkeypatch.setattr(review, "fresh_user", fresh_user)
    monkeypatch.setattr(company_transfers, "transfers", fixture.transfers)
    app = FastAPI()
    app.include_router(company_transfers.router, prefix="/api/company-admissions")
    app.dependency_overrides[get_current_user] = lambda: fixture.reviewer
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://transfer.test",
            timeout=5,
            event_hooks={"response": [capture]},
        ) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


async def request_surface(client: AsyncClient, surface: Surface) -> Response:
    method, path, decision = surface
    return await client.request(method, f"{BASE}/{path}", json={"decision": decision} if decision is not None else None)


@pytest.mark.parametrize("surface", SURFACES)
async def test_default_org_genesis_denied_payment_http(
    transfer_fixture: TransferFixture, client: AsyncClient, surface: Surface
) -> None:
    fixture = transfer_fixture
    fixture.users[fixture.reviewer.id] = replace(
        fixture.reviewer, role=UserRole.ORG_ADMIN, is_genesis=True, tenant_id=fixture.source.id
    )
    before = fixture.transfers.rows[REQUEST_ID]

    response = await request_surface(client, surface)

    assert response.status_code == 403, response.text
    assert fixture.transfers.rows[REQUEST_ID] == before
    assert not fixture.admissions.rows


@pytest.mark.parametrize("identity_flag", [False, True], ids=["role", "identity-flag"])
@pytest.mark.parametrize("surface", SURFACES)
async def test_canonical_platform_admin_retains_payment_http_access(
    transfer_fixture: TransferFixture, client: AsyncClient, surface: Surface, identity_flag: bool
) -> None:
    fixture = transfer_fixture
    identity = IdentityRecord(id=uuid4(), is_platform_admin=identity_flag)
    fixture.users[fixture.reviewer.id] = replace(
        fixture.reviewer,
        role=UserRole.MEMBER if identity_flag else UserRole.PLATFORM_ADMIN,
        identity_id=identity.id,
        identity=identity,
    )

    response = await request_surface(client, surface)

    assert response.status_code == 200, response.text
    match surface[2]:
        case None:
            if surface[1] == "review":
                rows = TypeAdapter(list[TransferSummary]).validate_json(response.content)
                assert [row.id for row in rows] == [REQUEST_ID]
            elif surface[1] == "review/page":
                page = TransferReviewPage.model_validate_json(response.content)
                assert [row.id for row in page.items] == [REQUEST_ID]
            else:
                assert TransferSummary.model_validate_json(response.content).id == REQUEST_ID
        case "approved" | "rejected" as decision:
            result = TransferSummary.model_validate_json(response.content)
            assert result.id == REQUEST_ID
            expected = {"approved": TransferStatus.ADMISSION_CREATED, "rejected": TransferStatus.REJECTED}[decision]
            assert result.status == expected
            assert result.reviewed_by_user_id == fixture.reviewer.id
            assert fixture.transfers.rows[REQUEST_ID].status == expected
            if result.admission_request_id is not None:
                with anyio.fail_after(1):
                    await fixture.email_ready.wait()
        case unreachable:
            assert_never(unreachable)


@pytest.mark.parametrize("gate", ["membership", "identity", "password", "role", "missing"])
@pytest.mark.parametrize("surface", SURFACES)
async def test_stale_session_cannot_bypass_fresh_payment_eligibility(
    transfer_fixture: TransferFixture, client: AsyncClient, surface: Surface, gate: str
) -> None:
    fixture = transfer_fixture
    identity = IdentityRecord(id=uuid4(), is_active=gate != "identity", must_change_password=gate == "password")
    fixture.users[fixture.reviewer.id] = replace(
        fixture.reviewer,
        role=UserRole.MEMBER if gate == "role" else UserRole.PLATFORM_ADMIN,
        is_active=gate != "membership",
        identity_id=identity.id,
        identity=identity,
    )
    if gate == "missing":
        del fixture.users[fixture.reviewer.id]
    before = fixture.transfers.rows[REQUEST_ID]

    response = await request_surface(client, surface)

    assert response.status_code == 403, response.text
    assert fixture.transfers.rows[REQUEST_ID] == before


async def test_applicant_retains_own_payment_detail(transfer_fixture: TransferFixture, client: AsyncClient) -> None:
    transfer_fixture.reviewer = transfer_fixture.applicant

    response = await client.get(f"{BASE}/{REQUEST_ID}")

    assert response.status_code == 200, response.text
    assert TransferSummary.model_validate_json(response.content).applicant_user_id == transfer_fixture.applicant.id


@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_payment_decision_rechecks_authority_after_locks(
    transfer_fixture: TransferFixture, client: AsyncClient, monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    fixture = transfer_fixture

    async def revoke(_db: DbConnection, _ids: Sequence[UUID | None]) -> None:
        fixture.users[fixture.reviewer.id] = replace(fixture.reviewer, role=UserRole.MEMBER)

    monkeypatch.setattr(review, "lock_tenants", revoke)
    before = fixture.transfers.rows[REQUEST_ID]

    response = await client.post(f"{BASE}/{REQUEST_ID}/decision", json={"decision": decision})

    assert response.status_code == 403, response.text
    assert fixture.transfers.rows[REQUEST_ID] == before
    assert not fixture.admissions.rows
