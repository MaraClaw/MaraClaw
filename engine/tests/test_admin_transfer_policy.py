from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI

from app.api import admin
from app.core.domain.users import UserRole
from app.core.security import create_access_token
from app.dao.identity_dao import identity_dao
from app.dao.user_dao import user_dao
from app.db.session import transaction
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from transfer_policy_fakes import MemoryPolicyConnection

INITIAL: dict[str, object] = {
    "fee_amount_minor": 3700,
    "currency": "EUR",
    "instructions": "Existing payment destination",
}
REPLACEMENT: dict[str, object] = {
    "fee_amount_minor": 0,
    "currency": "GBP",
    "instructions": "Updated payment destination",
}
URL = "/api/admin/transfer-policy"


@dataclass(slots=True)
class PolicyCase:
    db: MemoryPolicyConnection
    user: UserRecord
    client: httpx.AsyncClient


@pytest.fixture
async def policy_case(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[PolicyCase]:
    db = MemoryPolicyConnection(INITIAL)
    identity = IdentityRecord(id=uuid4(), email="operator@example.invalid", is_platform_admin=True)
    user = UserRecord(id=uuid4(), identity_id=identity.id, role=UserRole.PLATFORM_ADMIN, identity=identity)

    async def load_user(_id: UUID, *, fresh: bool = False) -> UserRecord:
        return user

    async def generation(_id: UUID) -> int:
        return 0

    async def bind_connection() -> AsyncIterator[None]:
        async with transaction(db=db):
            yield

    monkeypatch.setattr(user_dao, "get_with_identity", load_user)
    monkeypatch.setattr(identity_dao, "get_auth_generation", generation)
    app = FastAPI()
    app.include_router(admin.router, prefix="/api", dependencies=[Depends(bind_connection)])
    token = await create_access_token(str(user.id), user.role, identity_id=identity.id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {token}"
        yield PolicyCase(db=db, user=user, client=client)


async def test_platform_admin_reads_transfer_policy(policy_case: PolicyCase) -> None:
    # Given
    case = policy_case
    # When
    response = await case.client.get(URL)
    # Then
    assert response.status_code == 200
    assert response.json() == INITIAL
    assert case.db.events == ["read", "commit"]


async def test_platform_admin_replaces_policy_and_audits_same_transaction(policy_case: PolicyCase) -> None:
    # Given
    case = policy_case
    # When
    response = await case.client.put(URL, json=REPLACEMENT)
    # Then
    assert response.status_code == 200
    assert response.json() == REPLACEMENT
    assert case.db.committed_value == REPLACEMENT
    assert case.db.events == ["lock", "update", "audit", "commit"]
    assert len(case.db.committed_audits) == 1
    audit = case.db.committed_audits[0]
    assert (audit["actor_id"], audit["action"], audit["target_type"]) == (
        case.user.id,
        "transfer_policy_update",
        "transfer_policy",
    )
    assert audit["changes"] == {key: {"before": before, "after": REPLACEMENT[key]} for key, before in INITIAL.items()}


@pytest.mark.parametrize("role", ["org_admin", "member"])
@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_non_platform_admin_cannot_access_transfer_policy(
    policy_case: PolicyCase, role: str, method: str
) -> None:
    # Given: even a previously issued PA token must not retain revoked PA access.
    case = policy_case
    case.user.role = UserRole(role)
    assert case.user.identity is not None
    case.user.identity.is_platform_admin = False
    # When
    response = await case.client.request(method, URL, json=REPLACEMENT if method == "PUT" else None)
    # Then
    assert response.status_code == 403
    assert case.db.events == ["rollback"]


@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_force_password_change_blocks_transfer_policy(policy_case: PolicyCase, method: str) -> None:
    # Given
    case = policy_case
    assert case.user.identity is not None
    case.user.identity.must_change_password = True
    # When
    response = await case.client.request(method, URL, json=REPLACEMENT if method == "PUT" else None)
    # Then
    assert response.status_code == 403
    assert response.json()["detail"]["must_change_password"] is True
    assert case.db.events == ["rollback"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fee_amount_minor", -1),
        ("fee_amount_minor", 9007199254740992),
        ("fee_amount_minor", 1.0),
        ("fee_amount_minor", 1.5),
        ("fee_amount_minor", True),
        ("fee_amount_minor", "2500"),
        ("currency", "usd"),
        ("currency", "US"),
        ("currency", "US1"),
        ("currency", "USD\n"),
        ("currency", "CAD"),
        ("currency", "XXX"),
        ("instructions", " \n\t"),
        ("instructions", "x" * 4001),
        ("extra", "unknown"),
    ],
)
async def test_invalid_policy_cannot_mutate_settings(policy_case: PolicyCase, field: str, value: object) -> None:
    # Given
    case = policy_case
    body = {**REPLACEMENT, field: value}
    # When
    response = await case.client.put(URL, json=body)
    # Then
    assert response.status_code == 422
    assert case.db.value == INITIAL
    assert case.db.audits == []


async def test_replacement_requires_all_fields(policy_case: PolicyCase) -> None:
    # Given
    case = policy_case
    # When
    response = await case.client.put(URL, json={"fee_amount_minor": 900})
    # Then
    assert response.status_code == 422
    assert case.db.value == INITIAL


@pytest.mark.parametrize("currency", ["USD", "EUR", "GBP", "VND", "JPY", "KWD"])
@pytest.mark.parametrize("fee", [0, 9007199254740991])
async def test_supported_currency_and_safe_integer_bounds_round_trip(
    policy_case: PolicyCase, currency: str, fee: int
) -> None:
    # Given
    case = policy_case
    body = {**REPLACEMENT, "currency": currency, "fee_amount_minor": fee}
    # When
    response = await case.client.put(URL, json=body)
    # Then
    assert response.status_code == 200
    assert response.json() == body
    assert case.db.committed_value == body


async def test_failed_policy_update_rolls_back_without_audit(policy_case: PolicyCase) -> None:
    # Given
    case = policy_case
    case.db.update_error = RuntimeError("update rejected")
    # When
    with pytest.raises(RuntimeError, match="update rejected"):
        await case.client.put(URL, json=REPLACEMENT)
    # Then
    assert case.db.value == INITIAL
    assert case.db.audits == []
    assert case.db.events == ["lock", "update", "rollback"]
