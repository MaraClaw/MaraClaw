import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

import app.services.sso_scan_sessions as service
from app.core import security
from app.records.identity import IdentityRecord
from app.records.sso_scan_session import SSOScanSessionRecord
from app.records.user import UserRecord


class AtomicSessionDAO:
    def __init__(self, session: SSOScanSessionRecord | None) -> None:
        self.session = session
        self.lock = asyncio.Lock()

    async def get(self, session_id: UUID) -> SSOScanSessionRecord | None:
        row = self.session
        return replace(row) if row is not None and row.id == session_id else None

    async def authorize(
        self, session_id: UUID, *, user_id: UUID, provider_type: str, tenant_id: UUID | None
    ) -> SSOScanSessionRecord | None:
        async with self.lock:
            row = self.session
            if (
                row is None
                or row.id != session_id
                or row.status not in ("pending", "scanned")
                or row.expires_at <= datetime.now(UTC)
                or (row.tenant_id is not None and row.tenant_id != tenant_id)
            ):
                return None
            self.session = replace(row, status="authorized", user_id=user_id, provider_type=provider_type)
            return replace(self.session)

    async def redeem(self, session_id: UUID) -> UUID | None:
        async with self.lock:
            row = self.session
            if row is None or row.id != session_id or row.status != "authorized" or row.expires_at <= datetime.now(UTC):
                return None
            self.session = replace(row, status="completed")
            return row.user_id


def make_user(*, active: bool = True, identity_active: bool = True) -> UserRecord:
    identity = IdentityRecord(id=uuid4(), is_active=identity_active, must_change_password=True)
    return UserRecord(
        id=uuid4(), identity_id=identity.id, identity=identity, tenant_id=uuid4(), is_active=active, role="org_admin"
    )


def install_principal(monkeypatch: pytest.MonkeyPatch, user: UserRecord | None) -> list[bool]:
    fresh_reads: list[bool] = []

    async def get_with_identity(user_id: UUID, *, fresh: bool = False) -> UserRecord | None:
        assert user is None or user_id == user.id
        fresh_reads.append(fresh)
        return user

    monkeypatch.setattr(security.user_dao, "get_with_identity", get_with_identity)
    monkeypatch.setattr(security.user_dao, "get", AsyncMock(return_value=user))
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=7))
    return fresh_reads


@pytest.mark.parametrize("state", [None, "", "not-a-uuid", "undefined", "null", "[object Object]", "{}"])
def test_parse_scan_state_rejects_invalid_input(state: str | None) -> None:
    assert service.parse_scan_state(state) is None


@pytest.mark.parametrize("style", ["canonical", "uppercase", "hex", "urn"])
def test_parse_scan_state_accepts_uuid(style: str) -> None:
    session_id = uuid4()
    values = {
        "canonical": str(session_id),
        "uppercase": str(session_id).upper(),
        "hex": session_id.hex,
        "urn": session_id.urn,
    }
    assert service.parse_scan_state(values[style]) == session_id


@pytest.mark.parametrize("status", ["pending", "scanned", "authorized", "completed", "expired", "failed", "missing"])
@pytest.mark.parametrize("expired", [False, True])
async def test_load_authorizable_session_is_read_only(
    monkeypatch: pytest.MonkeyPatch, status: str, expired: bool
) -> None:
    row = SSOScanSessionRecord(
        id=uuid4(), status=status, expires_at=datetime.now(UTC) + timedelta(minutes=-5 if expired else 5)
    )
    dao = AtomicSessionDAO(None if status == "missing" else row)
    monkeypatch.setattr(service, "sso_scan_session_dao", dao)

    result = await service.load_authorizable_session(row.id)

    assert result == (row if status in ("pending", "scanned") and not expired else None)
    assert dao.session == (None if status == "missing" else row)


@pytest.mark.parametrize(
    ("status", "expired", "tenant", "expected"),
    [
        ("pending", False, "unbound", True),
        ("scanned", False, "same", True),
        ("pending", False, "other", False),
        ("pending", False, "tenantless_user", False),
        ("pending", True, "same", False),
        ("scanned", True, "same", False),
        ("authorized", False, "same", False),
        ("completed", False, "same", False),
        ("expired", False, "same", False),
    ],
)
async def test_authorize_respects_state_expiry_and_tenant(
    monkeypatch: pytest.MonkeyPatch, status: str, expired: bool, tenant: str, expected: bool
) -> None:
    user = make_user()
    tenant_id = None if tenant == "unbound" else uuid4() if tenant == "other" else user.tenant_id
    if tenant == "tenantless_user":
        user.tenant_id = None
    row = SSOScanSessionRecord(
        id=uuid4(),
        status=status,
        tenant_id=tenant_id,
        expires_at=datetime.now(UTC) + timedelta(minutes=-5 if expired else 5),
    )
    dao = AtomicSessionDAO(row)
    monkeypatch.setattr(service, "sso_scan_session_dao", dao)

    result = await service.authorize_scan_session(row.id, user=user, provider_type="feishu")

    assert result is expected
    assert dao.session == (
        replace(row, status="authorized", user_id=user.id, provider_type="feishu") if expected else row
    )
    assert dao.session is not None
    assert dao.session.access_token is None


async def test_concurrent_redeem_mints_one_fresh_login(monkeypatch: pytest.MonkeyPatch) -> None:
    user = make_user()
    row = SSOScanSessionRecord(
        id=uuid4(), status="authorized", user_id=user.id, expires_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    dao = AtomicSessionDAO(row)
    monkeypatch.setattr(service, "sso_scan_session_dao", dao)
    reads = install_principal(monkeypatch, user)

    results = await asyncio.gather(service.redeem_scan_session(row.id), service.redeem_scan_session(row.id))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].user == user
    claims = security.decode_access_token(winners[0].access_token)
    assert claims is not None
    assert claims["sub"] == str(user.id)
    assert claims["role"] == "org_admin"
    assert claims["gen"] == 7
    assert user.must_change_password
    assert reads == [True]
    assert dao.session == replace(row, status="completed")


@pytest.mark.parametrize("status", ["pending", "scanned", "completed", "expired", "missing"])
async def test_redeem_without_authorization_never_loads_user(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    row = SSOScanSessionRecord(id=uuid4(), status=status, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    monkeypatch.setattr(service, "sso_scan_session_dao", AtomicSessionDAO(None if status == "missing" else row))
    reads = install_principal(monkeypatch, None)

    result = await service.redeem_scan_session(row.id)

    assert result is None
    assert reads == []


async def test_expired_authorization_is_not_redeemed(monkeypatch: pytest.MonkeyPatch) -> None:
    row = SSOScanSessionRecord(id=uuid4(), status="authorized", expires_at=datetime.now(UTC) - timedelta(minutes=5))
    dao = AtomicSessionDAO(row)
    monkeypatch.setattr(service, "sso_scan_session_dao", dao)
    reads = install_principal(monkeypatch, None)

    result = await service.redeem_scan_session(row.id)

    assert result is None
    assert reads == []
    assert dao.session == row


@pytest.mark.parametrize("principal", ["inactive_user", "inactive_identity", "missing_identity", "missing_user"])
async def test_unavailable_principal_consumes_session(monkeypatch: pytest.MonkeyPatch, principal: str) -> None:
    user = make_user(active=principal != "inactive_user", identity_active=principal != "inactive_identity")
    if principal == "missing_identity":
        user.identity = None
    row = SSOScanSessionRecord(
        id=uuid4(), status="authorized", user_id=user.id, expires_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    dao = AtomicSessionDAO(row)
    monkeypatch.setattr(service, "sso_scan_session_dao", dao)
    reads = install_principal(monkeypatch, None if principal == "missing_user" else user)

    with pytest.raises(service.ScanLoginUnavailableError):
        await service.redeem_scan_session(row.id)

    assert reads == [True]
    assert dao.session == replace(row, status="completed")


async def test_repeated_redeem_returns_no_second_token(monkeypatch: pytest.MonkeyPatch) -> None:
    user = make_user()
    row = SSOScanSessionRecord(
        id=uuid4(), status="authorized", user_id=user.id, expires_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    monkeypatch.setattr(service, "sso_scan_session_dao", AtomicSessionDAO(row))
    reads = install_principal(monkeypatch, user)
    assert await service.redeem_scan_session(row.id) is not None

    result = await service.redeem_scan_session(row.id)

    assert result is None
    assert reads == [True]


async def test_redeem_mints_current_generation_after_older_credentials_are_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an authorized scan and an earlier credential for the same identity.
    user = make_user()
    row = SSOScanSessionRecord(
        id=uuid4(), status="authorized", user_id=user.id, expires_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    monkeypatch.setattr(service, "sso_scan_session_dao", AtomicSessionDAO(row))
    install_principal(monkeypatch, user)
    generation = AsyncMock(side_effect=[7, 8])
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", generation)
    earlier = await security.create_access_token(str(user.id), user.role)

    # When redemption follows revocation, then its new login reads SQL instead of retaining the old generation.
    result = await service.redeem_scan_session(row.id)
    assert result is not None
    assert security.decode_access_token(earlier)["gen"] == 7
    assert security.decode_access_token(result.access_token)["gen"] == 8
