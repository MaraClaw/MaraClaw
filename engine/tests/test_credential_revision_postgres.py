import json
from datetime import UTC, datetime
from uuid import UUID

import anyio
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import agent_credentials as api
from app.config import get_settings
from app.core.security import encrypt_data
from app.dao.agent_credential_dao import (
    CredentialChanges,
    CredentialRevisionConflictError,
    agent_credential_dao as credentials,
)
from app.db.connection import DbConnection
from app.db.errors import CheckViolationError
from app.db.pool import close_pool, get_pool, init_pool
from app.records.agent_credential import AgentCredentialRecord, CredentialRevision
from app.records.user import UserRecord
from app.schemas.agent_credential import CredentialCaptureReplace
from app.services.agent_credential_capture import CapturePlan, bind_capture, capture_cookies
from app.services.agent_credential_injection import inject_credentials
from credential_revision_fakes import COOKIE_SECRET, ScriptClient, idle
from test_openclaw_update_postgres import agent, db, member, tenant

__all__ = ["db"]


@pytest.fixture
async def credential(db: DbConnection) -> AgentCredentialRecord:
    company = await tenant(db)
    owner = await member(db, company)
    agent_id = await agent(db, company, owner)
    return await credentials.create(
        obj_in={
            "agent_id": agent_id,
            "platform": "portal.example",
            "display_name": "original",
            "cookies_json": encrypt_data(
                json.dumps([{"name": "session", "value": COOKIE_SECRET}]), get_settings().SECRET_KEY
            ),
        }
    )


async def test_revision_survives_reconnect_and_rejects_nonpositive_values(
    db: DbConnection, credential: AgentCredentialRecord
) -> None:
    assert credential.revision == 1
    conninfo = get_pool().conninfo
    assert isinstance(conninfo, str)
    await close_pool()
    _ = await init_pool(conninfo=conninfo, min_size=1, max_size=4)

    restored = await credentials.get_for_agent(credential.id, credential.agent_id)

    assert restored is not None
    assert restored.revision == 1
    with pytest.raises(CheckViolationError):
        await db.execute("UPDATE agent_credentials SET revision = 0 WHERE id = %(id)s", {"id": credential.id})


async def test_only_one_concurrent_editor_wins_and_evidence_is_cleared(credential: AgentCredentialRecord) -> None:
    target = CredentialRevision(credential.id, credential.agent_id, 1)
    assert await credentials.publish_injection(target, datetime.now(UTC))
    start = anyio.Event()
    outcomes: list[str] = []

    async def edit(name: str) -> None:
        await start.wait()
        try:
            result = await credentials.edit(target, {"display_name": name})
            outcomes.append(f"{result.display_name}:{result.revision}")
        except CredentialRevisionConflictError:
            outcomes.append("conflict")

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(edit, "first")
        tasks.start_soon(edit, "second")
        start.set()

    current = await credentials.get_for_agent(credential.id, credential.agent_id)
    assert outcomes.count("conflict") == 1
    assert current is not None
    assert f"{current.display_name}:2" in outcomes
    assert current.revision == 2
    assert current.last_injected_at is None
    assert current.cookies_json == credential.cookies_json


async def test_successful_publication_changes_only_injection_evidence(credential: AgentCredentialRecord) -> None:
    injected_at = datetime.now(UTC)

    published = await credentials.publish_injection(
        CredentialRevision(credential.id, credential.agent_id, 1), injected_at
    )

    assert published
    current = await credentials.get_for_agent(credential.id, credential.agent_id)
    assert current is not None
    assert current.last_injected_at == injected_at
    assert (current.revision, current.display_name, current.cookies_json, current.updated_at) == (
        1,
        credential.display_name,
        credential.cookies_json,
        credential.updated_at,
    )


@pytest.mark.parametrize("mutation", ["metadata", "cookies", "status", "delete"])
async def test_capture_cannot_restore_a_revision_changed_during_export(
    credential: AgentCredentialRecord, mutation: str
) -> None:
    target = await bind_capture(
        credential.agent_id,
        CredentialCaptureReplace(
            mode="replace",
            credential_id=credential.id,
            expected_revision=1,
        ),
    )
    client = ScriptClient()

    async def concurrent_edit() -> None:
        if mutation == "delete":
            _ = await credentials.delete(record_id=credential.id)
        else:
            changes: dict[str, CredentialChanges] = {
                "metadata": {"display_name": "newer"},
                "cookies": {"cookies_json": "newer-encrypted-value"},
                "status": {"status": "inactive"},
            }
            _ = await credentials.edit(CredentialRevision(credential.id, credential.agent_id, 1), changes[mutation])

    client.before_execute = concurrent_edit

    with pytest.raises(CredentialRevisionConflictError):
        await capture_cookies(client, CapturePlan(credential.agent_id, credential.platform, target))

    current = await credentials.get_for_agent(credential.id, credential.agent_id)
    if mutation == "delete":
        assert current is None
    else:
        assert current is not None
        assert current.revision == 2
        assert current.last_injected_at is None
        assert (
            getattr(current, {"metadata": "display_name", "cookies": "cookies_json", "status": "status"}[mutation])
            == {
                "metadata": "newer",
                "cookies": "newer-encrypted-value",
                "status": "inactive",
            }[mutation]
        )


@pytest.mark.parametrize("mutation", ["metadata", "cookies", "status", "delete"])
async def test_stale_injection_receipt_never_publishes_over_current_state(
    credential: AgentCredentialRecord, mutation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = ScriptClient()
    monkeypatch.setattr(client, "_ensure_browser_initialized", idle)

    async def concurrent_edit() -> None:
        if mutation == "delete":
            _ = await credentials.delete(record_id=credential.id)
        else:
            changes: dict[str, CredentialChanges] = {
                "metadata": {"display_name": "newer"},
                "cookies": {"cookies_json": "newer-encrypted-value"},
                "status": {"status": "inactive"},
            }
            _ = await credentials.edit(CredentialRevision(credential.id, credential.agent_id, 1), changes[mutation])

    client.before_execute = concurrent_edit

    await inject_credentials(client, credential.agent_id)

    current = await credentials.get_for_agent(credential.id, credential.agent_id)
    if mutation == "delete":
        assert current is None
    else:
        assert current is not None
        assert current.revision == 2
        assert current.last_injected_at is None


async def test_http_edits_require_current_revision_and_preserve_manage_only_vault(
    credential: AgentCredentialRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    access = "manage"

    async def check(_user: UserRecord, _agent: UUID) -> tuple[None, str]:
        return None, access

    async def current_user() -> UserRecord:
        return UserRecord(credential.agent_id)

    monkeypatch.setattr(api, "check_agent_access", check)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = current_user
    path = f"/agents/{credential.agent_id}/credentials/{credential.id}"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            changed = await http.put(path, json={"expected_revision": 1, "display_name": "edited"})
            assert changed.status_code == 200
            assert changed.json()["revision"] == 2
            assert COOKIE_SECRET not in changed.text
            assert "cookies_json" not in changed.json()
            conflict = await http.put(path, json={"expected_revision": 1, "cookies_json": "[]"})
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["code"] == "credential_revision_conflict"
            missing = await http.put(path, json={"cookies_json": COOKIE_SECRET})
            assert missing.status_code == 422
            assert COOKIE_SECRET not in missing.text
            access = "use"
            listing = await http.get(f"/agents/{credential.agent_id}/credentials/")
            assert listing.status_code == 403
            forbidden = await http.put(path, json={"expected_revision": 2, "display_name": "forbidden"})
            assert forbidden.status_code == 403
    finally:
        app.dependency_overrides.clear()
