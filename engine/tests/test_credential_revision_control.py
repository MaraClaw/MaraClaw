from uuid import UUID, uuid4

import pytest

from app.api import agentbay_control as control
from app.core.logging.service import flush_logging
from app.dao.agent_credential_dao import CredentialChanges, CredentialRevisionConflictError, agent_credential_dao
from app.records.agent_credential import AgentCredentialRecord, CredentialRevision
from app.services import take_control_state as state
from credential_revision_fakes import COOKIE_SECRET, ERROR_SECRET
from test_agentbay_control_runtime import ControlSurface, surface

__all__ = ["surface"]


@pytest.mark.parametrize("target", ["stale", "foreign", "missing"])
async def test_capture_replacement_rejects_before_export_and_releases_lease(
    surface: ControlSurface, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    credential_id = uuid4()

    async def find(_credential: UUID, _agent: UUID) -> AgentCredentialRecord | None:
        if target == "stale":
            return AgentCredentialRecord(credential_id, UUID(surface.key.agent_id), "portal.example", revision=2)
        return None

    monkeypatch.setattr(agent_credential_dao, "get_for_agent", find)
    surface.store.before_io = lambda: pytest.fail("invalid target reached browser export")

    response = await surface.post(
        "unlock",
        {
            "session_id": "chat",
            "owner_token": surface.token,
            "platform_hint": "portal.example",
            "capture": {"mode": "replace", "credential_id": str(credential_id), "expected_revision": 1},
        },
    )

    assert response.status_code == (409 if target == "stale" else 404)
    if target == "stale":
        assert response.json()["detail"]["code"] == "credential_revision_conflict"
    assert not await state.is_session_locked(surface.key.agent_id, "chat")
    assert surface.credentials == []


@pytest.mark.parametrize("release_failure", [False, True])
async def test_capture_race_conflict_remains_distinct_and_releases_owned_lease(
    surface: ControlSurface, monkeypatch: pytest.MonkeyPatch, release_failure: bool
) -> None:
    credential_id = uuid4()

    async def find(_credential: UUID, _agent: UUID) -> AgentCredentialRecord:
        return AgentCredentialRecord(credential_id, UUID(surface.key.agent_id), "portal.example")

    async def raced(_target: CredentialRevision, _changes: CredentialChanges) -> AgentCredentialRecord:
        if release_failure:
            surface.redis.advance(121)
        raise CredentialRevisionConflictError

    monkeypatch.setattr(agent_credential_dao, "get_for_agent", find)
    monkeypatch.setattr(agent_credential_dao, "edit", raced)

    response = await surface.post(
        "unlock",
        {
            "session_id": "chat",
            "owner_token": surface.token,
            "platform_hint": "portal.example",
            "capture": {"mode": "replace", "credential_id": str(credential_id), "expected_revision": 1},
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "credential_revision_conflict"
    assert not await state.is_session_locked(surface.key.agent_id, "chat")
    assert surface.credentials == []


async def test_first_capture_never_enumerates_or_replaces_existing_platform(
    surface: ControlSurface, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def enumerate_vault(_agent: UUID) -> list[AgentCredentialRecord]:
        pytest.fail("first capture must not enumerate the manager-only vault")

    async def replace(_target: CredentialRevision, _changes: CredentialChanges) -> AgentCredentialRecord:
        pytest.fail("explicit creation must not replace a platform match")

    monkeypatch.setattr(agent_credential_dao, "list_for_agent", enumerate_vault)
    monkeypatch.setattr(agent_credential_dao, "edit", replace)

    response = await surface.post(
        "unlock",
        {
            "session_id": "chat",
            "owner_token": surface.token,
            "platform_hint": "portal.example",
            "capture": {"mode": "create"},
        },
    )

    assert response.status_code == 200
    assert response.json()["cookies_exported"] is True
    assert response.json()["cookie_count"] == 1
    assert len(surface.credentials) == 1
    assert "secret" not in response.text


async def test_capture_failure_keeps_cookie_exception_details_private_and_releases_lease(
    surface: ControlSurface, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    async def failed_client(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(ERROR_SECRET)

    monkeypatch.setattr(control, "_get_client", failed_client)

    response = await surface.post(
        "unlock",
        {
            "session_id": "chat",
            "owner_token": surface.token,
            "platform_hint": "portal.example",
            "capture": {"mode": "create"},
        },
    )

    assert response.status_code == 200
    assert response.json()["cookies_exported"] is False
    assert COOKIE_SECRET not in response.text
    assert ERROR_SECRET not in response.text
    assert not await state.is_session_locked(surface.key.agent_id, "chat")
    flush_logging()
    captured = capfd.readouterr()
    assert COOKIE_SECRET not in captured.out + captured.err
    assert ERROR_SECRET not in captured.out + captured.err
