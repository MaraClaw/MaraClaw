import asyncio
import smtplib
import threading
from dataclasses import FrozenInstanceError
from email import message_from_string

import pytest

from app.db import session
from app.db.connection import DbConnection
from app.services import system_email_service as email


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> email.SystemEmailConfig:
    config = email.SystemEmailConfig(
        "from@example.com", "MaraClaw", "smtp.example.com", 465, "user", "secret", True, 42
    )

    async def resolve() -> email.SystemEmailConfig:
        return config

    monkeypatch.setattr(email, "resolve_email_config_async", resolve)
    return config


@pytest.mark.parametrize(
    ("failure", "kind", "error"),
    [
        (None, "sent", None),
        (smtplib.SMTPDataError(451, b"private server text"), "transient", "smtp_451"),
        (smtplib.SMTPDataError(550, b"private server text"), "permanent", "smtp_550"),
        (smtplib.SMTPAuthenticationError(535, b"secret"), "permanent", "smtp_535"),
        (smtplib.SMTPAuthenticationError(454, b"secret"), "transient", "smtp_454"),
        (smtplib.SMTPRecipientsRefused({"a@example.com": (550, b"private")}), "permanent", "recipient_refused"),
        (smtplib.SMTPRecipientsRefused({"a@example.com": (450, b"private")}), "transient", "recipient_refused"),
        (smtplib.SMTPServerDisconnected("private"), "transient", "SMTPServerDisconnected"),
        (TimeoutError("private"), "transient", "TimeoutError"),
        (OSError("private"), "transient", "OSError"),
        (smtplib.SMTPNotSupportedError("private"), "permanent", "SMTPNotSupportedError"),
        (ValueError("private"), "permanent", "ValueError"),
    ],
)
async def test_attempt_classifies_smtp_results(
    monkeypatch: pytest.MonkeyPatch,
    configured: email.SystemEmailConfig,
    failure: Exception | None,
    kind: str,
    error: str | None,
) -> None:
    # Given
    def smtp(**_kwargs: object) -> None:
        if failure is not None:
            raise failure

    monkeypatch.setattr(email, "send_smtp_email", smtp)
    # When
    outcome = await email.attempt_email("to@example.com", "subject", "body", message_id="<stable@example.com>")
    # Then
    assert (outcome.kind, outcome.error) == (kind, error)
    with pytest.raises(FrozenInstanceError):
        outcome.__setattr__("error", "changed")


async def test_missing_config_is_explicit_without_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    async def resolve() -> None:
        return None

    monkeypatch.setattr(email, "resolve_email_config_async", resolve)
    monkeypatch.setattr(email, "send_smtp_email", lambda **_kwargs: pytest.fail("SMTP without config"))
    # When
    outcome = await email.attempt_email("to@example.com", "subject", "body", message_id="<stable@example.com>")
    # Then
    assert outcome == email.EmailAttemptOutcome("not_configured", "not_configured")


async def test_broadcast_identity_timeout_and_db_context_are_isolated(
    monkeypatch: pytest.MonkeyPatch, configured: email.SystemEmailConfig
) -> None:
    # Given
    messages: list[str] = []
    timeouts: list[object] = []
    ambient = DbConnection.__new__(DbConnection)
    token = session._conn_ctx.set(ambient)

    async def resolve() -> email.SystemEmailConfig:
        assert session.get_connection() is None
        return configured

    def smtp(**kwargs: object) -> None:
        assert session.get_connection() is None
        message = kwargs["msg_string"]
        assert isinstance(message, str)
        messages.append(message)
        timeouts.append(kwargs["timeout"])

    monkeypatch.setattr(email, "resolve_email_config_async", resolve)
    monkeypatch.setattr(email, "send_smtp_email", smtp)
    # When
    try:
        for _ in range(2):
            await email.attempt_email("to@example.com", "subject", "body", message_id="<stable@example.com>")
        assert session.get_connection() is ambient
    finally:
        session._conn_ctx.reset(token)
    # Then
    assert [message_from_string(message)["Message-ID"] for message in messages] == ["<stable@example.com>"] * 2
    assert timeouts == [15, 15]
    assert configured.smtp_timeout_seconds == 42


async def test_non_broadcast_sends_keep_timeout_and_generate_identity(
    monkeypatch: pytest.MonkeyPatch, configured: email.SystemEmailConfig
) -> None:
    # Given
    messages: list[str] = []
    timeouts: list[object] = []

    def smtp(**kwargs: object) -> None:
        message = kwargs["msg_string"]
        assert isinstance(message, str)
        messages.append(message)
        timeouts.append(kwargs["timeout"])

    monkeypatch.setattr(email, "send_smtp_email", smtp)
    # When
    await email.send_system_email("to@example.com", "subject", "body")
    await email.send_system_email("to@example.com", "subject", "body")
    # Then
    ids = [message_from_string(message)["Message-ID"] for message in messages]
    assert ids[0]
    assert ids[1]
    assert ids[0] != ids[1]
    assert timeouts == [42, 42]


async def test_cancelled_attempt_does_not_wait_for_native_smtp_thread(
    monkeypatch: pytest.MonkeyPatch, configured: email.SystemEmailConfig
) -> None:
    # Given
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def smtp(**_kwargs: object) -> None:
        loop.call_soon_threadsafe(entered.set)
        try:
            release.wait(timeout=5)
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(email, "send_smtp_email", smtp)
    task = asyncio.create_task(email.attempt_email("to@example.com", "subject", "body", message_id="<id@example.com>"))
    # When
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        # Then
        assert not finished.is_set()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(finished.wait(), timeout=2)
