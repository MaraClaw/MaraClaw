import socketserver
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from typing import cast, override
from uuid import UUID

import pytest

from app.core.json_types import JsonObject
from app.dao.organization_admission_email_dao import organization_admission_email_dao as emails
from app.dao.tenant_dao import tenant_dao
from app.dao.tenant_email_dao import tenant_email_dao
from app.services import organization_admission_email_worker as worker
from app.services.organization_admission_credentials import unseal
from organization_admission_fixtures import OrganizationScenario, organization_scenario

__all__ = ["organization_scenario"]


class _Handler(socketserver.StreamRequestHandler):
    @override
    def handle(self) -> None:
        sink = cast("_Sink", self.server)
        self.wfile.write(b"220 sink\r\n")
        while line := self.rfile.readline(65536):
            verb = line.split(b" ", 1)[0].strip().upper()
            if verb in {b"EHLO", b"HELO"}:
                self.wfile.write(b"250 sink\r\n")
            elif verb == b"RCPT":
                self.wfile.write(sink.rcpt_reply)
            elif verb in {b"MAIL", b"RSET", b"NOOP"}:
                self.wfile.write(b"250 OK\r\n")
            elif verb == b"DATA":
                self.wfile.write(b"354 go\r\n")
                body = bytearray()
                while (chunk := self.rfile.readline(65536)) != b".\r\n":
                    body.extend(chunk)
                sink.messages.append(bytes(body))
                self.wfile.write(b"250 stored\r\n")
            elif verb == b"QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"502 no\r\n")


class _Sink(socketserver.ThreadingTCPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.daemon_threads = True
        self.messages: list[bytes] = []
        self.rcpt_reply: bytes = b"250 OK\r\n"


@pytest.fixture
def sink() -> Iterator[_Sink]:
    server = _Sink()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _settings(sink: _Sink) -> JsonObject:
    return {
        "enabled": True,
        "from_address": "noreply@example.com",
        "from_name": "MaraClaw",
        "smtp_host": "127.0.0.1",
        "smtp_port": sink.server_address[1],
        "smtp_username": "",
        "password": "",
        "smtp_ssl": False,
        "smtp_timeout_seconds": 5,
        "public_only": False,
    }


async def _approved_request(scenario: OrganizationScenario) -> UUID:
    _ = await scenario.submit()
    request_id = await scenario.pending_id()
    assert (await scenario.decide(request_id, scenario.genesis, {"decision": "approve"})).status_code == 200
    return request_id


async def _deliver_due() -> None:
    claims = await emails.claim_batch(
        limit=5, lease_seconds=180, max_attempts=worker.MAX_ATTEMPTS, now=datetime.now(UTC)
    )
    for claim in claims:
        await worker.deliver(claim)


async def _status(scenario: OrganizationScenario) -> dict[str, object]:
    row = await scenario.db.fetchone(
        "SELECT status,last_error,payload IS NULL AS payload_gone FROM organization_admission_credential_emails"
    )
    assert row is not None
    return row


async def test_platform_smtp_receives_the_credential_message_and_payload_is_deleted(
    organization_scenario: OrganizationScenario, sink: _Sink
):
    scenario = organization_scenario
    maraclaw = await tenant_dao.create(obj_in={"name": "MaraClaw", "slug": "maraclaw", "is_system": True})
    await tenant_email_dao.put(maraclaw.id, _settings(sink))
    request_id = await _approved_request(scenario)
    payload = await scenario.db.fetchval("SELECT payload FROM organization_admission_credential_emails")
    assert isinstance(payload, bytes)
    password = unseal(request_id, payload)
    await _deliver_due()
    [raw] = sink.messages
    message = message_from_bytes(raw, policy=policy.default)
    assert isinstance(message, EmailMessage)
    assert (message["To"], message["Message-ID"]) == (
        "buyer@example.com",
        f"<{request_id}.org-admission@maraclaw.local>",
    )
    text = message.get_body(("plain",))
    assert text is not None
    assert password in text.get_content()
    assert "Acme Robotics" in text.get_content()
    assert await _status(scenario) == {"status": "sent", "last_error": None, "payload_gone": True}


async def test_only_the_platform_smtp_is_ever_used_never_the_new_company(
    organization_scenario: OrganizationScenario, sink: _Sink
):
    scenario = organization_scenario
    _ = await tenant_dao.create(obj_in={"name": "MaraClaw", "slug": "maraclaw", "is_system": True})
    request_id = await _approved_request(scenario)
    created = await scenario.db.fetchval(
        "SELECT provisioned_tenant_id FROM organization_admission_requests WHERE id=%(id)s", {"id": request_id}
    )
    assert isinstance(created, UUID)
    await tenant_email_dao.put(created, _settings(sink))
    await _deliver_due()
    assert sink.messages == []
    assert await _status(scenario) == {"status": "pending", "last_error": "not_configured", "payload_gone": False}


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (b"550 no such user\r\n", ("failed", "recipient_refused", False)),
        (b"451 try later\r\n", ("pending", "recipient_refused", False)),
    ],
)
async def test_smtp_rejections_split_into_terminal_and_retryable(
    organization_scenario: OrganizationScenario, sink: _Sink, reply: bytes, expected: tuple[str, str, bool]
):
    scenario = organization_scenario
    maraclaw = await tenant_dao.create(obj_in={"name": "MaraClaw", "slug": "maraclaw", "is_system": True})
    await tenant_email_dao.put(maraclaw.id, _settings(sink))
    sink.rcpt_reply = reply
    _ = await _approved_request(scenario)
    await _deliver_due()
    assert sink.messages == []
    assert tuple((await _status(scenario)).values()) == expected
