import json
from datetime import datetime
from uuid import UUID, uuid4

import pytest

from app.config import get_settings
from app.core.logging.service import flush_logging
from app.core.security import encrypt_data
from app.records.agent_credential import AgentCredentialRecord, CredentialRevision
from app.services import agent_credential_injection as injection
from credential_revision_fakes import COOKIE_SECRET, ERROR_SECRET, ScriptClient, idle


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> ScriptClient:
    client = ScriptClient()
    monkeypatch.setattr(client, "_ensure_browser_initialized", idle)
    return client


async def test_injection_publishes_only_complete_nonempty_attributed_batches(
    monkeypatch: pytest.MonkeyPatch, client: ScriptClient, capfd: pytest.CaptureFixture[str]
) -> None:
    agent_id = uuid4()
    batches = [
        [{"name": "first", "value": COOKIE_SECRET}, {"name": "second", "value": COOKIE_SECRET}],
        [],
        [{"name": "reject", "value": COOKIE_SECRET}],
        [{"name": "first", "value": COOKIE_SECRET}, {"name": "reject", "value": COOKIE_SECRET}],
    ]
    credentials = [
        AgentCredentialRecord(
            uuid4(), agent_id, "portal.example", cookies_json=encrypt_data(json.dumps(batch), get_settings().SECRET_KEY)
        )
        for batch in batches
    ]
    credentials.extend(
        [
            AgentCredentialRecord(uuid4(), agent_id, "bad-encryption", cookies_json="invalid"),
            AgentCredentialRecord(
                uuid4(), agent_id, "bad-json", cookies_json=encrypt_data("{bad", get_settings().SECRET_KEY)
            ),
            AgentCredentialRecord(
                uuid4(), agent_id, "bad-shape", cookies_json=encrypt_data('["not-a-cookie"]', get_settings().SECRET_KEY)
            ),
        ]
    )
    published: list[CredentialRevision] = []

    async def active(_agent_id: UUID) -> list[AgentCredentialRecord]:
        return credentials

    async def publish(target: CredentialRevision, _at: datetime) -> bool:
        published.append(target)
        return True

    client.reject = ["reject"]
    monkeypatch.setattr(injection.agent_credential_dao, "list_active_with_cookies", active)
    monkeypatch.setattr(injection.agent_credential_dao, "publish_injection", publish)

    await injection.inject_credentials(client, agent_id)

    assert published == [CredentialRevision(credentials[0].id, agent_id, 1)]
    assert len(client.outputs) == 3
    assert all(COOKIE_SECRET not in output and ERROR_SECRET not in output for output in client.outputs)
    flush_logging()
    captured = capfd.readouterr()
    assert COOKIE_SECRET not in captured.out + captured.err
    assert ERROR_SECRET not in captured.out + captured.err


@pytest.mark.parametrize("failure", ["write", "exec", "missing", "malformed", "wrong-revision", "wrong-id", "zero"])
async def test_injection_never_publishes_unproven_receipts(
    monkeypatch: pytest.MonkeyPatch, client: ScriptClient, failure: str, capfd: pytest.CaptureFixture[str]
) -> None:
    agent_id = uuid4()
    credential = AgentCredentialRecord(
        uuid4(),
        agent_id,
        "portal.example",
        revision=4,
        cookies_json=encrypt_data(json.dumps(client.cookies), get_settings().SECRET_KEY),
    )

    async def active(_agent_id: UUID) -> list[AgentCredentialRecord]:
        return [credential]

    async def publish(_target: CredentialRevision, _at: datetime) -> bool:
        pytest.fail("unproven batch published injection evidence")

    receipt = {"credential_id": str(credential.id), "revision": 4, "accepted": 1, "failed": 0}
    if failure == "wrong-revision":
        receipt["revision"] = 3
    if failure == "wrong-id":
        receipt["credential_id"] = str(uuid4())
    if failure == "zero":
        receipt["accepted"] = 0
    stdout = "INJECT_RESULT:" + json.dumps(receipt)
    if failure == "missing":
        stdout = "INJECT_OK:1"
    if failure == "malformed":
        stdout = "INJECT_RESULT:{" + COOKIE_SECRET
    client.write_success = failure != "write"
    client.execution_override = {"success": failure != "exec", "stdout": stdout, "stderr": ERROR_SECRET}
    monkeypatch.setattr(injection.agent_credential_dao, "list_active_with_cookies", active)
    monkeypatch.setattr(injection.agent_credential_dao, "publish_injection", publish)

    await injection.inject_credentials(client, agent_id)

    flush_logging()
    captured = capfd.readouterr()
    assert COOKIE_SECRET not in captured.out + captured.err
    assert ERROR_SECRET not in captured.out + captured.err
