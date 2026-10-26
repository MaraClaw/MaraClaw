import json
from datetime import UTC, datetime

import pytest

from app.config import get_settings
from app.core.security import decrypt_data
from app.dao.agent_credential_dao import agent_credential_dao as credentials
from app.records.agent_credential import AgentCredentialRecord, CredentialRevision
from app.schemas.agent_credential import CredentialCaptureCreate, CredentialCaptureReplace
from app.services.agent_credential_capture import CapturePlan, bind_capture, capture_cookies
from credential_revision_fakes import COOKIE_SECRET, ScriptClient
from test_credential_revision_postgres import credential, db

__all__ = ["credential", "db"]


async def test_explicit_capture_replaces_only_the_bound_revision(credential: AgentCredentialRecord) -> None:
    target = await bind_capture(
        credential.agent_id,
        CredentialCaptureReplace(mode="replace", credential_id=credential.id, expected_revision=1),
    )
    assert target is not None
    assert await credentials.publish_injection(target, datetime.now(UTC))
    client = ScriptClient()

    count = await capture_cookies(client, CapturePlan(credential.agent_id, credential.platform, target))

    assert count == 1
    current = await credentials.get_for_agent(credential.id, credential.agent_id)
    assert current is not None
    assert current.revision == 2
    assert current.last_injected_at is None
    assert current.cookies_updated_at is not None
    assert current.last_login_at is not None
    assert current.display_name == "original"
    assert current.cookies_json is not None
    assert json.loads(decrypt_data(current.cookies_json, get_settings().SECRET_KEY)) == client.cookies


async def test_explicit_creation_preserves_an_existing_platform_credential(credential: AgentCredentialRecord) -> None:
    target = await bind_capture(credential.agent_id, CredentialCaptureCreate(mode="create"))
    client = ScriptClient()

    count = await capture_cookies(client, CapturePlan(credential.agent_id, credential.platform, target))

    assert count == 1
    rows = await credentials.list_for_agent(credential.agent_id)
    assert len(rows) == 2
    original = next(row for row in rows if row.id == credential.id)
    assert original == credential
    created = next(row for row in rows if row.id != credential.id)
    assert created.revision == 1
    assert created.platform == credential.platform
    assert created.last_injected_at is None
    assert created.cookies_json is not None
    assert COOKIE_SECRET not in created.cookies_json


@pytest.mark.parametrize("output", ["COOKIES_EXPORT:[]", "COOKIES_EXPORT:[", 'COOKIES_EXPORT:["bad"]'])
async def test_failed_capture_does_not_advance_or_clear_the_current_revision(
    credential: AgentCredentialRecord, output: str
) -> None:
    client = ScriptClient()
    client.execution_override = {"success": True, "stdout": output, "stderr": COOKIE_SECRET}
    target = CredentialRevision(credential.id, credential.agent_id, 1)
    injected_at = datetime.now(UTC)
    assert await credentials.publish_injection(target, injected_at)

    count = await capture_cookies(client, CapturePlan(credential.agent_id, credential.platform, target))

    assert count == 0
    current = await credentials.get_for_agent(credential.id, credential.agent_id)
    assert current is not None
    assert current.revision == 1
    assert current.cookies_json == credential.cookies_json
    assert current.last_injected_at == injected_at


async def test_current_but_inactive_revision_rejects_injection_evidence(credential: AgentCredentialRecord) -> None:
    current = await credentials.edit(CredentialRevision(credential.id, credential.agent_id, 1), {"status": "inactive"})

    published = await credentials.publish_injection(
        CredentialRevision(current.id, current.agent_id, current.revision), datetime.now(UTC)
    )

    assert published is False
    restored = await credentials.get_for_agent(credential.id, credential.agent_id)
    assert restored == current
