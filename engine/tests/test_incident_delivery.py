from dataclasses import replace
from unittest.mock import AsyncMock, Mock, call
from uuid import UUID

import pytest
from anyio import Event, create_task_group, fail_after

from app.core.domain.users import UserRole
from app.core.logging.adapter import Logger
from app.core.notification_kinds import NotificationKind
from app.db import session
from app.db.connection import DbConnection
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.services import incident_delivery as delivery
from app.services.incident_report import IncidentReport, build_report
from app.services.system_email_service import EmailAttemptOutcome


def admin(index: int, email: str | None = None) -> UserRecord:
    identity = IdentityRecord(id=UUID(int=index + 1000), email=email, is_platform_admin=True)
    return UserRecord(
        id=UUID(int=index),
        identity_id=identity.id,
        tenant_id=UUID(int=index + 2000),
        role=UserRole.PLATFORM_ADMIN,
        is_genesis=index == 1,
        identity=identity,
    )


@pytest.fixture
def report() -> IncidentReport:
    return build_report(RuntimeError("exception-secret"), source="test", operation="report-secret")


@pytest.fixture
def channels(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    notification = AsyncMock(return_value=None)
    email = AsyncMock(return_value=EmailAttemptOutcome("sent"))
    monkeypatch.setattr(delivery, "send_notification", notification)
    monkeypatch.setattr(delivery, "attempt_email", email)
    return notification, email


async def test_notifies_every_membership_when_admins_exceed_a_page(
    monkeypatch: pytest.MonkeyPatch, channels: tuple[AsyncMock, AsyncMock], report: IncidentReport
) -> None:
    # Given 251 email-less PAs across tenants, including two sharing an identity.
    users = [admin(index) for index in range(1, 252)]
    users[1] = replace(users[0], id=users[1].id, tenant_id=users[1].tenant_id, is_genesis=False)
    monkeypatch.setattr(delivery.user_dao, "list_active_platform_admins", AsyncMock(return_value=users))
    # When the incident is delivered.
    result = await delivery.deliver_incident(report)
    # Then each membership gets its own untargeted system alert, with no email required.
    assert result is True
    assert channels[0].await_count == len(users)
    channels[0].assert_has_awaits(
        [
            call(
                user.id,
                notification_kind=NotificationKind.SYSTEM_ALERT,
                title=report.title,
                body=report.body,
                sender_name="System",
            )
            for user in users
        ],
        any_order=True,
    )
    channels[1].assert_not_awaited()


async def test_deduplicates_casefold_addresses_when_memberships_overlap(
    monkeypatch: pytest.MonkeyPatch, channels: tuple[AsyncMock, AsyncMock], report: IncidentReport
) -> None:
    # Given Unicode casefold equivalents and a distinct address.
    users = [admin(1, "Straße@example.com"), admin(2, "STRASSE@EXAMPLE.COM"), admin(3, "other@example.com")]
    monkeypatch.setattr(delivery.user_dao, "list_active_platform_admins", AsyncMock(return_value=users))
    ids = Mock(side_effect=["<first@example.com>", "<second@example.com>"])
    monkeypatch.setattr(delivery, "make_msgid", ids)
    # When the incident is delivered.
    await delivery.deliver_incident(report)
    # Then unique mailboxes get independent messages while all memberships are notified.
    assert channels[0].await_count == 3
    assert channels[1].await_count == 2
    channels[1].assert_has_awaits(
        [
            call(to=users[0].email, subject=report.title, body=report.body, message_id="<first@example.com>"),
            call(to=users[2].email, subject=report.title, body=report.body, message_id="<second@example.com>"),
        ],
        any_order=True,
    )


@pytest.mark.parametrize("notification_fails", [False, True])
@pytest.mark.parametrize(
    "email_result",
    [
        EmailAttemptOutcome("sent"),
        EmailAttemptOutcome("not_configured"),
        EmailAttemptOutcome("permanent"),
        EmailAttemptOutcome("transient"),
        RuntimeError("email-secret"),
    ],
)
async def test_combines_channel_outcomes_when_one_channel_fails(
    monkeypatch: pytest.MonkeyPatch,
    channels: tuple[AsyncMock, AsyncMock],
    report: IncidentReport,
    notification_fails: bool,
    email_result: EmailAttemptOutcome | Exception,
) -> None:
    # Given independently failing channels for one addressable PA.
    monkeypatch.setattr(
        delivery.user_dao, "list_active_platform_admins", AsyncMock(return_value=[admin(1, "a@example.com")])
    )
    if notification_fails:
        channels[0].side_effect = RuntimeError("notification-secret")
    if isinstance(email_result, Exception):
        channels[1].side_effect = email_result
    else:
        channels[1].return_value = email_result
    # When the incident is delivered.
    result = await delivery.deliver_incident(report)
    # Then both channels are attempted and only a real success counts.
    assert result is (not notification_fails or email_result == EmailAttemptOutcome("sent"))
    channels[0].assert_awaited_once()
    channels[1].assert_awaited_once()


async def test_continues_other_recipients_when_a_recipient_fails(
    monkeypatch: pytest.MonkeyPatch, channels: tuple[AsyncMock, AsyncMock], report: IncidentReport
) -> None:
    # Given the first recipient fails both channels and the second succeeds in-app only.
    users = [admin(1, "a@example.com"), admin(2, "b@example.com")]
    monkeypatch.setattr(delivery.user_dao, "list_active_platform_admins", AsyncMock(return_value=users))
    channels[0].side_effect = [RuntimeError("notification-secret"), None]
    channels[1].side_effect = RuntimeError("email-secret")
    # When the incident is delivered.
    result = await delivery.deliver_incident(report)
    # Then neither a peer failure nor an earlier success short-circuits the remaining attempts.
    assert result is True
    assert channels[0].await_count == channels[1].await_count == 2


@pytest.mark.parametrize("recipients", [[], RuntimeError("query-secret")])
async def test_handles_discovery_when_empty_or_failed(
    monkeypatch: pytest.MonkeyPatch,
    channels: tuple[AsyncMock, AsyncMock],
    report: IncidentReport,
    recipients: list[UserRecord] | Exception,
) -> None:
    # Given either no active admins or a failed lookup (which is not an empty audience).
    query = AsyncMock()
    if isinstance(recipients, Exception):
        query.side_effect = recipients
    else:
        query.return_value = recipients
    monkeypatch.setattr(delivery.user_dao, "list_active_platform_admins", query)
    # When delivery resolves its recipients.
    result = await delivery.deliver_incident(report)
    # Then only an empty audience is successful without any channel attempt.
    assert result is (recipients == [])
    channels[0].assert_not_awaited()
    channels[1].assert_not_awaited()


async def test_logs_only_safe_metadata_when_channels_fail(
    monkeypatch: pytest.MonkeyPatch, channels: tuple[AsyncMock, AsyncMock], report: IncidentReport
) -> None:
    # Given secrets in recipients, exception text, report metadata and outcome error details.
    users = [admin(1, "private-address@example.com"), admin(2, "second-address@example.com")]
    monkeypatch.setattr(delivery.user_dao, "list_active_platform_admins", AsyncMock(return_value=users))
    channels[0].side_effect = RuntimeError("exception-secret")
    channels[1].side_effect = [RuntimeError("exception-secret"), EmailAttemptOutcome("transient", "outcome-secret")]
    warning = Mock()
    bind = Mock(return_value=Mock(spec=Logger, warning=warning))
    log = Mock(spec=Logger, bind=bind)
    monkeypatch.setattr(delivery, "logger", log)
    # When both channels fail.
    result = await delivery.deliver_incident(report)
    # Then diagnostics expose only fixed messages and exception types/outcome kinds, never tracebacks.
    assert result is False
    assert bind.call_args_list == [
        call(exception_type="RuntimeError"),
        call(exception_type="RuntimeError"),
        call(exception_type="RuntimeError"),
        call(outcome_kind="transient"),
    ]
    assert warning.call_args_list == [
        call("Incident notification delivery failed"),
        call("Incident email delivery failed"),
        call("Incident notification delivery failed"),
        call("Incident email delivery unsuccessful"),
    ]


@pytest.mark.parametrize("channel", [0, 1], ids=["notification", "email"])
async def test_bounds_recipients_without_truncating_when_channels_wait(
    monkeypatch: pytest.MonkeyPatch, channels: tuple[AsyncMock, AsyncMock], report: IncidentReport, channel: int
) -> None:
    # Given nine recipients and one blocked channel; the adapter fake accepts its real call fields.
    users = [admin(index, f"{index}@example.com") for index in range(1, 10)]
    monkeypatch.setattr(delivery.user_dao, "list_active_platform_admins", AsyncMock(return_value=users))
    entered, release = Event(), Event()
    active = peak = 0
    results: list[bool] = []

    async def stall(*_args: UUID, **_kwargs: str) -> EmailAttemptOutcome:
        nonlocal active, peak
        assert session.get_connection() is None
        active += 1
        peak = max(peak, active)
        if active == 4:
            entered.set()
        try:
            await release.wait()
        finally:
            active -= 1
        return EmailAttemptOutcome("sent")

    channels[channel].side_effect = stall

    async def deliver() -> None:
        results.append(await delivery.deliver_incident(report))

    # When four recipients occupy the delivery slots, then they are released.
    with fail_after(2):
        async with create_task_group() as tasks:
            tasks.start_soon(deliver)
            await entered.wait()
            release.set()
    # Then only four ran concurrently, and every recipient still received both attempts.
    assert peak == 4
    assert active == 0
    assert results == [True]
    assert channels[0].await_count == channels[1].await_count == len(users)


async def test_releases_dao_transactions_before_email_when_consumer_context_is_fresh(
    monkeypatch: pytest.MonkeyPatch, channels: tuple[AsyncMock, AsyncMock], report: IncidentReport
) -> None:
    # Given DAO seams using the real transaction context manager, without live PostgreSQL.
    commits = AsyncMock()
    db = AsyncMock(spec=DbConnection, commit=commits)
    observations: list[tuple[DbConnection | None, int]] = []

    async def query() -> list[UserRecord]:
        async with session.transaction(db):
            return [admin(1, "a@example.com")]

    async def store(_user_id: UUID, **_fields: str) -> None:
        async with session.transaction(db):
            pass

    async def email(**_fields: str) -> EmailAttemptOutcome:
        observations.append((session.get_connection(), commits.await_count))
        return EmailAttemptOutcome("sent")

    monkeypatch.setattr(delivery.user_dao, "list_active_platform_admins", query)
    channels[0].side_effect = store
    channels[1].side_effect = email
    # When a fresh-context consumer delivers an incident.
    result = await delivery.deliver_incident(report)
    # Then the lookup and notification transactions are both finished before email.
    assert result is True
    assert observations == [(None, 2)]
    channels[1].assert_awaited_once()
