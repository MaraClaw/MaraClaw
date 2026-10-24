from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.transaction_pool_fakes import CountingPool

from app.api import auth
from app.core.json_types import json_object_from_response
from app.db import session
from app.db.connection import DbConnection, Params, Row
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.schemas.schemas import UserUpdate


@dataclass
class _ProfileState:
    row: Row
    updates: list[dict[str, object]] = field(default_factory=list)
    session_bump: AsyncMock = field(default_factory=AsyncMock)


@pytest.fixture
def profile_api(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, _ProfileState]:
    identity = IdentityRecord(id=uuid4(), email="member@example.com", email_verified=True)
    user_id = uuid4()
    state = _ProfileState(
        row={
            "id": user_id,
            "identity_id": identity.id,
            "tenant_id": uuid4(),
            "display_name": "Member",
            "role": "member",
            "is_active": True,
            "registration_source": "web",
            "created_at": datetime(2026, 10, 24, tzinfo=UTC),
            "timezone": "Asia/Shanghai",
            "i_id": identity.id,
            "i_email": identity.email,
            "i_is_active": True,
            "i_email_verified": True,
        }
    )

    async def fetchone(_db: DbConnection, query: str, params: Params = None) -> Row:
        if query.startswith("UPDATE users SET "):
            assert isinstance(params, Mapping)
            changes = {key: value for key, value in params.items() if key != "id"}
            state.updates.append(changes)
            state.row.update(changes)
        else:
            assert query.startswith("SELECT ")
            assert "FROM users u" in query
        return state.row.copy()

    async def principal() -> UserRecord:
        user = await auth.user_dao.get_with_identity(user_id, fresh=True)
        assert user is not None
        return user

    monkeypatch.setattr(session, "get_pool", lambda: CountingPool())
    monkeypatch.setattr(DbConnection, "fetchone", fetchone)
    monkeypatch.setattr(import_module("app.dao.user_dao"), "bump_user_session", state.session_bump)
    application = FastAPI()
    application.include_router(auth.router, prefix="/api")
    application.dependency_overrides[auth.get_current_user] = principal
    application.dependency_overrides[auth.get_authenticated_user] = principal
    return application, state


@pytest.mark.parametrize("timezone", ["UTC", "Etc/GMT-7", "Asia/Shanghai", None])
async def test_patch_me_persists_member_timezone(
    profile_api: tuple[FastAPI, _ProfileState], timezone: str | None
) -> None:
    # Given an existing member profile with a saved timezone.
    application, state = profile_api
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        # When the member saves a timezone or explicitly clears it.
        response = await client.patch("/api/auth/me", json={"timezone": timezone})

    # Then the member row and response agree, with the existing session invalidation.
    assert response.status_code == 200
    assert state.updates == [{"timezone": timezone}]
    assert state.row["timezone"] == timezone
    assert json_object_from_response(response)["timezone"] == timezone
    state.session_bump.assert_awaited_once_with(state.row["id"])


@pytest.mark.parametrize("timezone", ["UTC", "Etc/GMT-7", "Asia/Shanghai", None])
async def test_get_me_reads_member_timezone(profile_api: tuple[FastAPI, _ProfileState], timezone: str | None) -> None:
    # Given a persisted timezone preference, including an unset preference.
    application, state = profile_api
    state.row["timezone"] = timezone
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        # When a new profile read loads the user through the DAO join.
        response = await client.get("/api/auth/me")

    # Then the response exposes the stored value without substituting UTC.
    assert response.status_code == 200
    assert json_object_from_response(response)["timezone"] == timezone


@pytest.mark.parametrize("timezone", ["", " ", "Mars/Olympus", "GMT+7", "../UTC"])
async def test_patch_me_rejects_invalid_timezone(profile_api: tuple[FastAPI, _ProfileState], timezone: str) -> None:
    # Given a member with a valid existing preference.
    application, state = profile_api
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        # When an invalid or empty timezone is submitted.
        response = await client.patch("/api/auth/me", json={"timezone": timezone})

    # Then validation identifies the field and accepted format without writing anything.
    assert response.status_code == 422
    details = json_object_from_response(response)["detail"]
    assert isinstance(details, list)
    detail = details[0]
    assert isinstance(detail, dict)
    assert detail["loc"] == ["body", "timezone"]
    message = detail["msg"]
    assert isinstance(message, str)
    assert "IANA" in message
    assert state.updates == []
    assert state.row["timezone"] == "Asia/Shanghai"


async def test_patch_me_omitted_timezone_preserves_preference(profile_api: tuple[FastAPI, _ProfileState]) -> None:
    # Given a member whose saved timezone differs from the frontend UTC fallback.
    application, state = profile_api
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        # When another profile field is edited without a timezone key.
        response = await client.patch("/api/auth/me", json={"display_name": "Updated"})

    # Then only that field is written and the saved timezone remains visible.
    assert response.status_code == 200
    assert state.updates == [{"display_name": "Updated"}]
    assert json_object_from_response(response)["timezone"] == "Asia/Shanghai"


def test_user_update_distinguishes_unset_timezone_from_clear() -> None:
    # Given omitted and explicit-null preference inputs.
    omitted, cleared = {}, {"timezone": None}
    # When the PATCH payloads are parsed and projected.
    payloads = [UserUpdate.model_validate(value).model_dump(exclude_unset=True) for value in (omitted, cleared)]
    # Then omission does not overwrite a stored preference while null clears it.
    assert payloads == [{}, {"timezone": None}]


def test_user_record_defaults_timezone_to_unset() -> None:
    # Given a legacy row that has no timezone value.
    row = {"id": uuid4(), "registration_source": "web"}
    # When the record is loaded.
    user = UserRecord.from_row(row)
    # Then the nullable preference is unset.
    assert user.timezone is None


def test_fresh_users_schema_has_nullable_timezone() -> None:
    # Given the fresh-only schema baseline.
    baseline = Path(__file__).resolve().parents[1] / "scripts" / "schema_baseline.sql"
    # When the users table's column definitions are read.
    columns = baseline.read_text().split("CREATE TABLE IF NOT EXISTS users (", 1)[1].split("\n);", 1)[0]
    timezone = [line.strip() for line in columns.splitlines() if line.strip().startswith("timezone ")]
    # Then the member preference is nullable with no server-side UTC default.
    assert timezone == ["timezone VARCHAR(100),"]
