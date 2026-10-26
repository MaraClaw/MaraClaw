import json
from datetime import UTC, datetime

import pytest

from app.config import get_settings
from app.core.security import encrypt_data
from app.dao.agent_credential_dao import CredentialRevisionConflictError, agent_credential_dao as credentials
from app.db.connection import DbConnection
from app.db.session import transaction
from app.records.agent_credential import AgentCredentialRecord, CredentialRevision
from app.schemas.agent_credential import CredentialCaptureReplace
from app.services.agent_credential_capture import CapturePlan, bind_capture, capture_cookies
from app.services.agent_credential_injection import InjectionReceipt, inject_credentials
from credential_revision_fakes import ScriptClient, idle
from test_credential_revision_postgres import credential, db

__all__ = ["credential", "db"]


class TransactionAbortedError(Exception):
    pass


async def persisted_credential(db: DbConnection, credential: AgentCredentialRecord) -> AgentCredentialRecord:
    row = await db.fetchone(
        "SELECT * FROM agent_credentials WHERE id = %(id)s AND agent_id = %(agent_id)s",
        {"id": credential.id, "agent_id": credential.agent_id},
    )
    assert row is not None
    return AgentCredentialRecord.from_row(row)


async def deactivate_and_reactivate(credential: AgentCredentialRecord) -> AgentCredentialRecord:
    inactive = await credentials.edit(CredentialRevision(credential.id, credential.agent_id, 1), {"status": "inactive"})
    assert (inactive.revision, inactive.status) == (2, "inactive")
    active = await credentials.edit(CredentialRevision(credential.id, credential.agent_id, 2), {"status": "active"})
    assert (active.revision, active.status) == (3, "active")
    return active


async def test_delayed_injection_cannot_publish_when_credential_is_deactivated_then_reactivated(
    credential: AgentCredentialRecord, db: DbConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = ScriptClient()
    monkeypatch.setattr(client, "_ensure_browser_initialized", idle)
    reactivated: list[AgentCredentialRecord] = []

    async def concurrent_edit() -> None:
        reactivated.append(await deactivate_and_reactivate(credential))

    client.before_execute = concurrent_edit

    await inject_credentials(client, credential.agent_id)

    assert len(client.outputs) == 1
    receipt = InjectionReceipt.model_validate_json(client.outputs[0].removeprefix("INJECT_RESULT:"))
    assert (receipt.credential_id, receipt.revision, receipt.accepted, receipt.failed) == (credential.id, 1, 1, 0)
    current = await persisted_credential(db, credential)
    assert current == reactivated[0]
    assert (current.revision, current.status, current.last_injected_at) == (3, "active", None)
    assert current.cookies_json == credential.cookies_json


async def test_bound_capture_conflicts_when_credential_is_deactivated_then_reactivated(
    credential: AgentCredentialRecord, db: DbConnection
) -> None:
    target = await bind_capture(
        credential.agent_id,
        CredentialCaptureReplace(mode="replace", credential_id=credential.id, expected_revision=1),
    )
    assert target is not None
    client = ScriptClient()
    client.cookies = [{"name": "session", "value": "delayed-browser-cookie"}]
    reactivated: list[AgentCredentialRecord] = []

    async def concurrent_edit() -> None:
        reactivated.append(await deactivate_and_reactivate(credential))

    client.before_execute = concurrent_edit

    with pytest.raises(CredentialRevisionConflictError):
        await capture_cookies(client, CapturePlan(credential.agent_id, credential.platform, target))

    current = await persisted_credential(db, credential)
    assert current == reactivated[0]
    assert (current.revision, current.status, current.last_injected_at) == (3, "active", None)
    assert current.cookies_json == credential.cookies_json


async def test_edit_restores_revision_cookies_and_evidence_when_owning_transaction_aborts(
    credential: AgentCredentialRecord, db: DbConnection
) -> None:
    target = CredentialRevision(credential.id, credential.agent_id, 1)
    injected_at = datetime(2026, 1, 1, tzinfo=UTC)
    assert await credentials.publish_injection(target, injected_at)
    before = await persisted_credential(db, credential)
    replacement = encrypt_data(
        json.dumps([{"name": "session", "value": "uncommitted-cookie"}]), get_settings().SECRET_KEY
    )
    assert replacement != before.cookies_json

    async def aborted_edit() -> None:
        async with transaction() as owner:
            assert owner.raw is not db.raw
            edited = await credentials.edit(target, {"cookies_json": replacement})
            assert (edited.revision, edited.cookies_json, edited.last_injected_at) == (2, replacement, None)
            raise TransactionAbortedError

    with pytest.raises(TransactionAbortedError):
        await aborted_edit()

    restored = await persisted_credential(db, credential)
    assert restored == before
    assert (restored.revision, restored.cookies_json, restored.last_injected_at) == (
        1,
        credential.cookies_json,
        injected_at,
    )


async def test_publication_restores_prior_evidence_when_owning_transaction_aborts(
    credential: AgentCredentialRecord, db: DbConnection
) -> None:
    target = CredentialRevision(credential.id, credential.agent_id, 1)
    prior = datetime(2026, 1, 1, tzinfo=UTC)
    replacement = datetime(2026, 1, 2, tzinfo=UTC)
    assert await credentials.publish_injection(target, prior)
    before = await persisted_credential(db, credential)

    async def aborted_publication() -> None:
        async with transaction() as owner:
            assert owner.raw is not db.raw
            assert await credentials.publish_injection(target, replacement)
            published = await credentials.get_for_agent(credential.id, credential.agent_id)
            assert published is not None
            assert published.last_injected_at == replacement
            raise TransactionAbortedError

    with pytest.raises(TransactionAbortedError):
        await aborted_publication()

    restored = await persisted_credential(db, credential)
    assert restored == before
    assert restored.last_injected_at == prior
