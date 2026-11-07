import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import anyio
import pytest
from psycopg import OperationalError
from pydantic import TypeAdapter

from app.dao.organization_admission_email_dao import organization_admission_email_dao as emails
from app.db.errors import DbError
from app.records.organization_admission_email import CredentialEmailClaim
from app.runtime.tasks import OwnedTasks
from app.services import organization_admission_email_worker as worker
from app.services.organization_admission_credentials import unseal
from app.services.system_email_service import EmailAttemptOutcome
from organization_admission_fixtures import OrganizationScenario, organization_scenario
from progress_fakes import FakeClock, Pacer

__all__ = ["organization_scenario"]

SENT = EmailAttemptOutcome("sent")
NAME = "Acme <b>Robotics</b> & Sons"
EXPECTED_ID = "<{}.org-admission@maraclaw.local>"


@dataclass
class Mailbox:
    outcomes: list[EmailAttemptOutcome] = field(default_factory=list)
    calls: list[tuple[str, str, str, str]] = field(default_factory=list)

    async def __call__(self, to: str, subject: str, body: str, *, message_id: str) -> EmailAttemptOutcome:
        self.calls.append((to, subject, body, message_id))
        return self.outcomes.pop(0) if self.outcomes else SENT


@pytest.fixture
def mailbox(monkeypatch: pytest.MonkeyPatch) -> Mailbox:
    box = Mailbox()
    monkeypatch.setattr(worker, "attempt_email", box)
    return box


async def _approved(scenario: OrganizationScenario) -> UUID:
    _ = await scenario.submit(name=NAME)
    request_id = await scenario.pending_id()
    assert (await scenario.decide(request_id, scenario.genesis, {"decision": "approve"})).status_code == 200
    return request_id


async def _drain(*, due: bool = True) -> int:
    claims = await emails.claim_batch(
        limit=5, lease_seconds=180, max_attempts=worker.MAX_ATTEMPTS, now=datetime.now(UTC)
    )
    for claim in claims:
        await worker.deliver(claim)
    return len(claims)


async def _make_due(scenario: OrganizationScenario) -> None:
    await scenario.db.execute(
        "UPDATE organization_admission_credential_emails SET available_at = now() - interval '1 second'"
    )


async def _row(scenario: OrganizationScenario) -> dict[str, object]:
    row = await scenario.db.fetchone("SELECT * FROM organization_admission_credential_emails")
    assert row is not None
    return row


async def _item(scenario: OrganizationScenario, request_id: UUID) -> dict[str, object]:
    items = TypeAdapter(list[dict[str, object]]).validate_python(
        (await scenario.review(scenario.genesis, status="approved")).json()["items"]
    )
    return next(item for item in items if item["id"] == str(request_id))


async def _password(scenario: OrganizationScenario) -> str:
    row = await _row(scenario)
    request_id, payload = row["request_id"], row["payload"]
    assert isinstance(request_id, UUID)
    assert isinstance(payload, bytes)
    return unseal(request_id, payload)


async def test_delivery_sends_one_plain_text_message_then_deletes_the_payload(
    organization_scenario: OrganizationScenario, mailbox: Mailbox
):
    scenario = organization_scenario
    request_id = await _approved(scenario)
    password = await _password(scenario)
    assert await _drain() == 1
    [(to, subject, body, message_id)] = mailbox.calls
    assert (to, message_id) == ("buyer@example.com", EXPECTED_ID.format(request_id))
    assert subject
    assert "\n" not in subject
    assert NAME not in subject
    assert all(part in body for part in (NAME, "buyer@example.com", password))
    assert "http" not in body
    assert "choose a new password" in body
    row = await _row(scenario)
    assert (row["status"], row["payload"], row["last_error"], row["claim_token"], row["attempts"]) == (
        "sent",
        None,
        None,
        None,
        1,
    )
    item = await _item(scenario, request_id)
    assert (item["credential_email_status"], item["credential_email_error"]) == ("sent", None)
    assert await _drain() == 0
    assert len(mailbox.calls) == 1
    replay = await scenario.decide(request_id, scenario.genesis, {"decision": "approve"})
    assert replay.json()["credential_email_status"] == "sent"
    assert len(mailbox.calls) == 1
    assert (await scenario.retry_email(request_id, scenario.genesis)).status_code == 409


async def test_transient_failure_backs_off_and_resends_the_same_credential(
    organization_scenario: OrganizationScenario, mailbox: Mailbox
):
    scenario = organization_scenario
    request_id = await _approved(scenario)
    mailbox.outcomes = [EmailAttemptOutcome("transient", "TimeoutError")]
    assert await _drain() == 1
    row = await _row(scenario)
    assert (row["status"], row["last_error"], row["attempts"]) == ("pending", "TimeoutError", 1)
    assert isinstance(row["available_at"], datetime)
    assert row["available_at"] > datetime.now(UTC)
    assert row["payload"] is not None
    assert await _drain() == 0
    item = await _item(scenario, request_id)
    assert item["credential_email_status"] == "pending"
    await _make_due(scenario)
    assert await _drain() == 1
    assert mailbox.calls[0] == mailbox.calls[1]
    assert (await _row(scenario))["status"] == "sent"


@pytest.mark.parametrize(
    "outcome",
    [EmailAttemptOutcome("permanent", "recipient_refused"), EmailAttemptOutcome("not_configured", "not_configured")],
)
async def test_terminal_failure_is_visible_and_operator_retry_resends_the_same_intent(
    organization_scenario: OrganizationScenario, mailbox: Mailbox, outcome: EmailAttemptOutcome
):
    scenario = organization_scenario
    request_id = await _approved(scenario)
    attempts = 1 if outcome.kind == "permanent" else worker.MAX_ATTEMPTS
    mailbox.outcomes = [outcome] * attempts
    for _ in range(attempts):
        await _make_due(scenario)
        assert await _drain() == 1
    assert len(mailbox.calls) == attempts
    item = await _item(scenario, request_id)
    assert (item["credential_email_status"], item["credential_email_error"]) == ("failed", outcome.error)
    first_message = mailbox.calls[0]
    retried = await scenario.retry_email(request_id, scenario.genesis)
    assert (retried.status_code, retried.json()["credential_email_status"]) == (200, "pending")
    assert (await scenario.retry_email(request_id, scenario.genesis)).json()["credential_email_status"] == "pending"
    row = await _row(scenario)
    assert (row["attempts"], row["last_error"], row["message_id"]) == (0, None, EXPECTED_ID.format(request_id))
    assert await _drain() == 1
    assert mailbox.calls[-1] == first_message
    assert (await _row(scenario))["status"] == "sent"


async def test_crashed_claim_is_recovered_after_its_lease_expires(
    organization_scenario: OrganizationScenario, mailbox: Mailbox
):
    scenario = organization_scenario
    _ = await _approved(scenario)
    claimed = await emails.claim_batch(
        limit=5, lease_seconds=180, max_attempts=worker.MAX_ATTEMPTS, now=datetime.now(UTC)
    )
    assert len(claimed) == 1
    assert await _drain() == 0
    await scenario.db.execute(
        "UPDATE organization_admission_credential_emails SET lease_expires_at = now() - interval '1 second'"
    )
    assert await _drain() == 1
    assert await emails.mark_sent(claimed[0], now=datetime.now(UTC)) is False
    row = await _row(scenario)
    assert (row["status"], row["attempts"], len(mailbox.calls)) == ("sent", 2, 1)


async def test_worker_started_after_restart_delivers_intents_committed_before_it(
    organization_scenario: OrganizationScenario, mailbox: Mailbox, monkeypatch: pytest.MonkeyPatch
):
    scenario = organization_scenario
    _ = await _approved(scenario)
    settled = anyio.Event()
    mark_sent = emails.mark_sent

    async def mark_then_signal(claim: CredentialEmailClaim, *, now: datetime) -> bool:
        result = await mark_sent(claim, now=now)
        settled.set()
        return result

    monkeypatch.setattr(emails, "mark_sent", mark_then_signal)
    async with anyio.create_task_group() as group:
        group.start_soon(worker.start_organization_admission_email_worker)
        with anyio.fail_after(30):
            await settled.wait()
        group.cancel_scope.cancel()
    assert len(mailbox.calls) == 1
    assert (await _row(scenario))["status"] == "sent"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("UPDATE identities SET must_change_password=false WHERE email='buyer@example.com'", "password_changed"),
        (
            "UPDATE identities SET password_hash='$2b$12$reset-by-an-operator' WHERE email='buyer@example.com'",
            "password_changed",
        ),
        ("UPDATE identities SET is_active=false WHERE email='buyer@example.com'", "account_unavailable"),
        (
            "UPDATE organization_admission_credential_emails SET payload='\\x00112233445566778899aabbccddeeff00112233'",
            "payload_unreadable",
        ),
    ],
)
async def test_obsolete_or_unreadable_credentials_are_never_sent_and_not_retryable(
    organization_scenario: OrganizationScenario, mailbox: Mailbox, mutation: str, code: str
):
    scenario = organization_scenario
    request_id = await _approved(scenario)
    await scenario.db.execute(mutation)
    assert await _drain() == 1
    assert mailbox.calls == []
    row = await _row(scenario)
    assert (row["status"], row["last_error"], row["payload"]) == ("failed", code, None)
    item = await _item(scenario, request_id)
    assert (item["credential_email_status"], item["credential_email_error"]) == ("failed", code)
    retried = await scenario.retry_email(request_id, scenario.genesis)
    assert (retried.status_code, retried.json()["credential_email_status"]) == (200, "failed")
    assert await _drain() == 0
    assert mailbox.calls == []


async def test_claim_db_errors_retry_with_capped_backoff_and_readiness_tracks_beats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the real loop as a budgeted owned task whose first claims fail (no database needed).
    clock, pacer = FakeClock(), Pacer()
    tasks = OwnedTasks(clock)
    failures: list[Exception] = [DbError("down"), OperationalError("down"), OSError("down")]
    claims: list[int] = []

    async def claim_batch(**_kwargs: object) -> list[CredentialEmailClaim]:
        if failures:
            raise failures.pop(0)
        claims.append(1)
        return []

    monkeypatch.setattr(emails, "claim_batch", claim_batch)
    monkeypatch.setattr(worker, "sleep", pacer.sleep)
    tasks.start(
        "organization_admission_email",
        worker.start_organization_admission_email_worker(),
        requirement="loop",
        progress_budget_seconds=worker.PROGRESS_BUDGET_SECONDS,
    )
    try:
        await pacer.parked()
        # When the budget lapses on failed attempts alone, the loop is stale but still running.
        clock.now += worker.PROGRESS_BUDGET_SECONDS + 1
        assert not tasks.ready()
        assert not tasks.required["organization_admission_email"][0].done()
        await pacer.release()
        await pacer.release()
        assert not tasks.ready()
        # Then a finished pass beats and readiness recovers, after backoff 1, 2, 4 and the poll sleep.
        await pacer.release()
        assert tasks.ready()
        assert pacer.waits == [1.0, 2.0, 4.0, worker.POLL_SECONDS]
        assert claims == [1]
        assert worker.DB_RETRY_CAP_SECONDS < worker.PROGRESS_BUDGET_SECONDS
    finally:
        await tasks.stop()


async def test_batch_timeout_stays_fatal_and_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a claim whose delivery never settles and an already-expired batch deadline.
    claim_count = 0
    stuck = asyncio.Event()

    async def claim_batch(**_kwargs: object) -> list[CredentialEmailClaim]:
        nonlocal claim_count
        claim_count += 1
        return [
            CredentialEmailClaim(
                request_id=uuid4(),
                identity_id=uuid4(),
                to_email="buyer@example.com",
                credential_fingerprint="fingerprint",
                payload=b"",
                message_id="<stuck@example.com>",
                organization_name=NAME,
                attempts=1,
                deliver_by=datetime.now(UTC),
                claim_token=uuid4(),
            )
        ]

    async def deliver(_claim: CredentialEmailClaim) -> None:
        await stuck.wait()

    @contextmanager
    def expired(_seconds: int) -> Generator[None]:
        with anyio.fail_after(0):
            yield

    monkeypatch.setattr(emails, "claim_batch", claim_batch)
    monkeypatch.setattr(worker, "deliver", deliver)
    monkeypatch.setattr(worker, "fail_after", expired)
    # When the loop runs, then the timeout ends it and nothing restarts it.
    with pytest.raises((ExceptionGroup, TimeoutError)):
        await worker.start_organization_admission_email_worker()
    assert claim_count == 1
