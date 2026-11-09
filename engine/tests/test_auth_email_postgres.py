import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from typing import assert_never
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from anyio import to_thread
from cryptography.exceptions import InvalidTag
from pydantic import TypeAdapter
from redis.exceptions import ConnectionError as RedisConnectionError
from tests.auth_email_fixtures import AuthEmailScenario, MailSink, scenario, sink

from app.dao.auth_email_dao import AuthEmailDAO, auth_email_dao as emails
from app.dao.tenant_email_dao import tenant_email_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.auth_email import AuthEmailKind
from app.services import auth_email_delivery as delivery, auth_email_worker as worker, password_reset_service as reset
from app.services.email_verification_service import email_verification_service as verification

__all__ = ["scenario", "sink"]


def _message(raw: bytes) -> tuple[str, str]:
    message = message_from_bytes(raw, policy=policy.default)
    assert isinstance(message, EmailMessage)
    body = message.get_body(("plain",))
    assert body is not None
    message_id = TypeAdapter(str).validate_python(message["Message-ID"])
    return message_id, TypeAdapter(str).validate_python(body.get_content())


@pytest.mark.parametrize("kind", [AuthEmailKind.VERIFICATION, AuthEmailKind.RESET])
async def test_committed_job_survives_restart_and_delivers_original_credential(
    scenario: AuthEmailScenario, sink: MailSink, kind: AuthEmailKind
):
    job_id, credential, expiry = await scenario.prepare(kind)
    [claim] = await AuthEmailDAO().claim_batch()
    assert claim.kind is kind
    assert credential.encode() not in claim.payload
    assert b"trusted.example" not in claim.payload
    assert claim.expires_at == expiry
    await worker.deliver(claim)
    [raw] = sink.messages
    message_id, body = _message(raw)
    assert message_id == f"<{job_id}.auth-email@maraclaw.local>"
    assert credential in body
    assert "https://trusted.example/" in body
    assert await scenario.state() == {"status": "sent", "last_error": None, "payload_gone": True, "attempts": 1}
    match kind:
        case AuthEmailKind.VERIFICATION:
            assert await verification.consume_email_verification_token(credential) == {
                "identity_id": scenario.identity.id,
                "email": scenario.identity.email,
            }
        case AuthEmailKind.RESET:
            assert await reset.consume_password_reset_token(credential) == {"identity_id": scenario.identity.id}
        case unreachable:
            assert_never(unreachable)


@pytest.mark.parametrize("field", ["id", "identity_id", "kind", "expires_at"])
async def test_encryption_binds_protected_material_to_job_metadata(scenario: AuthEmailScenario, field):
    await scenario.prepare()
    [claim] = await emails.claim_batch()
    changes = {
        "id": uuid4(),
        "identity_id": uuid4(),
        "kind": AuthEmailKind.VERIFICATION,
        "expires_at": datetime.now(UTC),
    }
    with pytest.raises(InvalidTag):
        delivery.unseal(replace(claim, **{field: changes[field]}))


async def test_competing_workers_claim_bounded_disjoint_jobs_and_expired_claims_are_fenced(scenario: AuthEmailScenario):
    for _ in range(7):
        await scenario.prepare()
    first, second = await asyncio.gather(emails.claim_batch(), AuthEmailDAO().claim_batch())
    assert sorted((len(first), len(second))) == [2, 5]
    assert len({claim.id for claim in (*first, *second)}) == 7
    assert await emails.claim_batch() == []
    old = first[0]
    await scenario.db.execute(
        "UPDATE auth_email_jobs SET lease_expires_at=now()-INTERVAL '1 second' WHERE id=%(id)s", {"id": old.id}
    )
    [fresh] = await AuthEmailDAO().claim_batch()
    assert fresh.id == old.id
    assert fresh.claim_token != old.claim_token
    assert not await emails.settle(old, None)
    assert await emails.settle(fresh, "test_terminal")


@pytest.mark.parametrize("kind", [AuthEmailKind.VERIFICATION, AuthEmailKind.RESET])
@pytest.mark.parametrize("obsolete", ["consumed", "superseded", "password", "email", "inactive", "expired"])
async def test_obsolete_credentials_are_never_sent(
    scenario: AuthEmailScenario, sink: MailSink, kind: AuthEmailKind, obsolete
):
    _, credential, _ = await scenario.prepare(kind)
    match obsolete:
        case "consumed":
            match kind:
                case AuthEmailKind.VERIFICATION:
                    await verification.consume_email_verification_token(credential)
                case AuthEmailKind.RESET:
                    await reset.consume_password_reset_token(credential)
                case unreachable:
                    assert_never(unreachable)
        case "superseded":
            match kind:
                case AuthEmailKind.VERIFICATION:
                    await verification.create_email_verification_token(
                        scenario.identity.id, scenario.identity.email or ""
                    )
                case AuthEmailKind.RESET:
                    await reset.create_password_reset_token(scenario.identity.id)
                case unreachable:
                    assert_never(unreachable)
        case "password":
            await scenario.db.execute("UPDATE identities SET password_hash='changed'")
        case "email":
            await scenario.db.execute("UPDATE identities SET email='changed@example.com'")
        case "inactive":
            await scenario.db.execute("UPDATE identities SET is_active=false")
        case "expired":
            await scenario.db.execute("UPDATE auth_email_jobs SET expires_at=now()-INTERVAL '1 second'")
    for claim in await emails.claim_batch():
        await worker.deliver(claim)
    assert sink.messages == []
    state = await scenario.state()
    assert state["status"] == "failed"
    assert state["payload_gone"] is True


async def test_verified_identity_suppresses_stale_verification(scenario: AuthEmailScenario, sink: MailSink):
    await scenario.prepare(AuthEmailKind.VERIFICATION)
    await scenario.db.execute("UPDATE identities SET email_verified=true")
    [claim] = await emails.claim_batch()
    await worker.deliver(claim)
    assert sink.messages == []
    assert (await scenario.state())["last_error"] == "credential_obsolete"


async def test_redis_uncertainty_defers_then_retries_same_material(
    scenario: AuthEmailScenario, sink: MailSink, monkeypatch
):
    _, credential, expiry = await scenario.prepare()
    [claim] = await emails.claim_batch()
    with monkeypatch.context() as patch:
        patch.setattr(delivery, "get_redis", AsyncMock(side_effect=RedisConnectionError("unavailable")))
        await worker.deliver(claim)
    assert sink.messages == []
    assert await scenario.state() == {
        "status": "pending",
        "last_error": "eligibility_unavailable",
        "payload_gone": False,
        "attempts": 1,
    }
    await scenario.due()
    [retry] = await emails.claim_batch()
    assert retry.expires_at == expiry
    await worker.deliver(retry)
    assert credential in _message(sink.messages[0])[1]


@pytest.mark.parametrize(("reply", "status"), [(b"451 retry\r\n", "pending"), (b"550 missing\r\n", "failed")])
async def test_smtp_outcomes_drive_retry_and_terminal_scrubbing(
    scenario: AuthEmailScenario, sink: MailSink, reply, status
):
    await scenario.prepare()
    sink.rcpt_reply = reply
    [claim] = await emails.claim_batch()
    await worker.deliver(claim)
    assert sink.messages == []
    state = await scenario.state()
    assert (state["status"], state["last_error"], state["payload_gone"]) == (
        status,
        "recipient_refused",
        status == "failed",
    )


async def test_configuration_loss_defers_without_accepting_smtp(scenario: AuthEmailScenario, sink: MailSink):
    await scenario.prepare()
    await tenant_email_dao.delete(scenario.platform_id)
    [claim] = await emails.claim_batch()
    await worker.deliver(claim)
    assert sink.messages == []
    assert (await scenario.state())["last_error"] == "not_configured"


async def test_smtp_lost_acknowledgement_replays_the_same_message_and_expiry(
    scenario: AuthEmailScenario, sink: MailSink
):
    _, credential, expiry = await scenario.prepare()
    sink.lose_ack = True
    [claim] = await emails.claim_batch()
    await worker.deliver(claim)
    assert (await scenario.state())["status"] == "pending"
    sink.lose_ack = False
    await scenario.due()
    [retry] = await AuthEmailDAO().claim_batch()
    assert retry.expires_at == expiry
    await worker.deliver(retry)
    assert len(sink.messages) == 2
    first, second = map(_message, sink.messages)
    assert first == second
    assert credential in first[1]
    assert (await scenario.state())["payload_gone"] is True


async def test_cancelled_smtp_batch_drains_native_sender_before_returning(scenario: AuthEmailScenario, sink: MailSink):
    await scenario.prepare()
    [claim] = await emails.claim_batch()
    sink.release.clear()
    task = asyncio.create_task(worker.deliver(claim))
    try:
        assert await to_thread.run_sync(lambda: sink.received.wait(5))
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        sink.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await scenario.state())["status"] == "pending"


async def test_rollback_never_exposes_jobs_and_forgot_ack_is_already_committed(
    scenario: AuthEmailScenario, sink: MailSink
):
    async def rolled_back_producer():
        async with connection_ctx():
            await scenario.prepare()
            assert await emails.claim_batch() == []
            raise RuntimeError("producer rollback")

    with pytest.raises(RuntimeError):
        await rolled_back_producer()
    assert await emails.claim_batch() == []
    response = await scenario.client.post("/auth/forgot-password", json={"email": scenario.identity.email})
    assert response.status_code == 200
    [claim] = await AuthEmailDAO().claim_batch()
    assert sink.messages == []
    await worker.deliver(claim)
    assert (await scenario.state())["status"] == "sent"


async def test_unverified_http_login_commits_before_403(scenario: AuthEmailScenario, monkeypatch):
    await user_dao.create(obj_in={"identity_id": scenario.identity.id, "role": "member", "display_name": "Member"})
    monkeypatch.setattr("app.api.auth.verify_password_async", AsyncMock(return_value=True))
    response = await scenario.client.post(
        "/auth/login", json={"login_identifier": scenario.identity.email, "password": "secret"}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["needs_verification"] is True
    [claim] = await AuthEmailDAO().claim_batch()
    assert delivery.unseal(claim).to_email == scenario.identity.email


@pytest.mark.parametrize("kind", [AuthEmailKind.VERIFICATION, AuthEmailKind.RESET])
async def test_unreadable_redis_credentials_defer_without_smtp(
    scenario: AuthEmailScenario, sink: MailSink, kind: AuthEmailKind
):
    _, credential, _ = await scenario.prepare(kind)
    match kind:
        case AuthEmailKind.VERIFICATION:
            prefix = "email_verify:token:"
        case AuthEmailKind.RESET:
            prefix = "pwd_reset:token:"
        case unreachable:
            assert_never(unreachable)
    await scenario.redis.set(f"{prefix}{hashlib.sha256(credential.encode()).hexdigest()}", "unreadable")
    [claim] = await emails.claim_batch()
    await worker.deliver(claim)
    assert sink.messages == []
    assert (await scenario.state())["last_error"] == "eligibility_unavailable"


async def test_retry_exhaustion_scrubs_original_bearer_material(scenario: AuthEmailScenario, sink: MailSink):
    await scenario.prepare()
    sink.rcpt_reply = b"451 retry\r\n"
    for _ in range(5):
        await scenario.due()
        [claim] = await emails.claim_batch()
        await worker.deliver(claim)
    assert await emails.claim_batch() == []
    assert await scenario.state() == {
        "status": "failed",
        "last_error": "recipient_refused",
        "payload_gone": True,
        "attempts": 5,
    }
