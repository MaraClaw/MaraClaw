import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

import app.services.sso_scan_sessions as service
from app.api import sso
from app.core import security
from app.records.identity import IdentityRecord
from app.records.sso_scan_session import SSOScanSessionRecord
from app.records.user import UserRecord


class StatusDAO:
    def __init__(self, row: SSOScanSessionRecord | None, *, concurrent: bool = False) -> None:
        self.row = row
        self.expirations = 0
        self.scans = 0
        self.reads = 0
        self.barrier = asyncio.Barrier(2) if concurrent else None

    async def get(self, session_id: UUID) -> SSOScanSessionRecord | None:
        assert self.row is None or self.row.id == session_id
        snapshot = replace(self.row) if self.row is not None else None
        self.reads += 1
        if self.barrier is not None and self.reads <= 2:
            await self.barrier.wait()
        return snapshot

    async def expire(self, session_id: UUID) -> SSOScanSessionRecord | None:
        self.expirations += 1
        row = self.row
        assert row is not None
        assert row.id == session_id
        if row.status in ("pending", "scanned", "authorized") and row.expires_at <= datetime.now(UTC):
            self.row = replace(row, status="expired")
            return replace(self.row)
        return None

    async def redeem(self, session_id: UUID) -> UUID | None:
        row = self.row
        assert row is not None
        assert row.id == session_id
        if row.status != "authorized" or row.expires_at <= datetime.now(UTC):
            return None
        self.row = replace(row, status="completed")
        return row.user_id

    async def mark_scanned(self, session_id: UUID) -> SSOScanSessionRecord | None:
        self.scans += 1
        row = self.row
        assert row is None or row.id == session_id
        if row is not None and row.status == "pending" and row.expires_at > datetime.now(UTC):
            self.row = replace(row, status="scanned")
            return replace(self.row)
        return None


def install_session(
    monkeypatch: pytest.MonkeyPatch, *, status: str = "authorized", expired: bool = False, concurrent: bool = False
) -> tuple[StatusDAO, UserRecord, UUID]:
    identity = IdentityRecord(id=uuid4())
    user = UserRecord(
        id=uuid4(), identity=identity, identity_id=identity.id, created_at=datetime.now(UTC), tenant_id=uuid4()
    )
    session_id = uuid4()
    row = SSOScanSessionRecord(
        id=session_id,
        status=status,
        user_id=user.id,
        tenant_id=user.tenant_id,
        provider_type="feishu",
        expires_at=datetime.now(UTC) + timedelta(minutes=-5 if expired else 5),
    )
    dao = StatusDAO(row, concurrent=concurrent)
    monkeypatch.setattr(sso, "sso_scan_session_dao", dao)
    monkeypatch.setattr(service, "sso_scan_session_dao", dao)

    async def get_with_identity(user_id: UUID, *, fresh: bool = False) -> UserRecord:
        assert user_id == user.id
        assert fresh
        return user

    monkeypatch.setattr(security.user_dao, "get_with_identity", get_with_identity)
    return dao, user, session_id


async def test_winning_poll_preserves_login_response(monkeypatch: pytest.MonkeyPatch) -> None:
    dao, user, session_id = install_session(monkeypatch)

    response = await sso.get_sso_session_status(session_id)

    assert response["status"] == "authorized"
    assert response["provider_type"] == "feishu"
    assert response["error_msg"] is None
    token = response["access_token"]
    assert isinstance(token, str)
    claims = security.decode_access_token(token)
    assert claims is not None
    assert claims["sub"] == str(user.id)
    assert response["user"] == sso.UserOut.model_validate(user).model_dump()
    assert dao.row is not None
    assert dao.row.status == "completed"
    assert dao.row.access_token is None


async def test_later_poll_has_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, session_id = install_session(monkeypatch)
    assert "access_token" in await sso.get_sso_session_status(session_id)

    response = await sso.get_sso_session_status(session_id)

    assert response == {"status": "completed", "provider_type": "feishu", "error_msg": None}


async def test_concurrent_poll_has_exactly_one_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, session_id = install_session(monkeypatch, concurrent=True)

    responses = await asyncio.gather(sso.get_sso_session_status(session_id), sso.get_sso_session_status(session_id))

    assert sum("access_token" in response for response in responses) == 1
    assert {response["status"] for response in responses} == {"authorized", "completed"}
    assert all("user" not in response for response in responses if response["status"] == "completed")


@pytest.mark.parametrize("status", ["pending", "scanned", "completed", "expired", "failed"])
async def test_non_authorized_poll_has_no_token(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    _, _, session_id = install_session(monkeypatch, status=status)

    response = await sso.get_sso_session_status(session_id)

    assert response == {"status": status, "provider_type": "feishu", "error_msg": None}


@pytest.mark.parametrize("status", ["pending", "scanned", "authorized", "completed", "failed"])
async def test_expiry_is_conditional_and_preserves_terminal_states(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    dao, _, session_id = install_session(monkeypatch, status=status, expired=True)

    response = await sso.get_sso_session_status(session_id)

    expected = "expired" if status in ("pending", "scanned", "authorized") else status
    assert response == {"status": expected, "provider_type": "feishu", "error_msg": None}
    assert dao.expirations == 1


@pytest.mark.parametrize("inactive", ["user", "identity"])
async def test_inactive_principal_is_consumed_without_token(monkeypatch: pytest.MonkeyPatch, inactive: str) -> None:
    dao, user, session_id = install_session(monkeypatch)
    if inactive == "user":
        user.is_active = False
    else:
        assert user.identity is not None
        user.identity.is_active = False

    response = await sso.get_sso_session_status(session_id)

    assert response == {"status": "failed", "provider_type": "feishu", "error_msg": "Unable to sign in"}
    assert dao.row is not None
    assert dao.row.status == "completed"


async def test_missing_session_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sso, "sso_scan_session_dao", StatusDAO(None))

    with pytest.raises(HTTPException) as error:
        await sso.get_sso_session_status(uuid4())

    assert error.value.status_code == 404


@pytest.mark.parametrize("status", ["pending", "scanned", "authorized", "completed", "expired"])
@pytest.mark.parametrize("expired", [False, True])
async def test_scan_marker_uses_conditional_transition(
    monkeypatch: pytest.MonkeyPatch, status: str, expired: bool
) -> None:
    dao, _, session_id = install_session(monkeypatch, status=status, expired=expired)

    response = await sso.mark_sso_session_scanned(session_id)

    assert response == {"status": "ok"}
    assert dao.scans == 1
    assert dao.row is not None
    assert dao.row.status == ("scanned" if status == "pending" and not expired else status)
