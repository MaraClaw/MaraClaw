"""Agent email runs its blocking work on its own thread lane, never on the event loop or a shared thread pool.

The IMAP exchanges of `read_emails`, `reply_email` and the connection test, the SMTP sends and the attachment reads
all run on `AGENT_EMAIL_LANE`. A fake `imaplib.IMAP4_SSL` parks the exchange in its login until a coroutine running on
the event loop releases it, so a loop that is held by the exchange fails the test instead of passing by timing.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from collections.abc import Callable, Coroutine
from functools import partial
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services import email_service
from app.services.email_service import EmailConfig
from test_email_service_attachments import FakeIMAP, RangedStorage, RecordingStorage, _settings_without_dotenv
from thread_lane_fixtures import forbid_shared_threads

LANE = "agent-email-lane"
CONFIG: EmailConfig = {
    "email_address": "agent@example.test",
    "auth_code": "code",
    "smtp_host": "127.0.0.1",
    "smtp_port": 2525,
    "smtp_ssl": False,
}


async def read(config: EmailConfig) -> str:
    return await email_service.read_emails(config)


async def reply(config: EmailConfig) -> str:
    return await email_service.reply_email(config, "<orig@example.test>", "Reply body")


async def probe(config: EmailConfig) -> str:
    result = await email_service.test_connection(config)
    return f"{result.get('imap', '')}; {result.get('smtp', '')}"


# The call, the start of the text it returns, and the socket timeout its IMAP and SMTP exchanges use.
CALLS = [
    pytest.param(read, "\U0001f4ec 1 email(s) from INBOX", 15, 0, id="read_emails"),
    pytest.param(reply, "\u2705 Reply sent to alice@example.test", 15, 1, id="reply_email"),
    pytest.param(
        probe, "\u2705 IMAP connected (1 emails in INBOX); \u2705 SMTP connected", 10, 1, id="test_connection"
    ),
]


class Exchanges:
    """The thread name and socket timeout of every IMAP login and SMTP send a call made."""

    def __init__(self) -> None:
        self.imap: list[tuple[str, float | None]] = []
        self.smtp: list[tuple[str, object]] = []

    def on_login(self, imap: FakeIMAP) -> None:
        self.imap.append((threading.current_thread().name, imap.timeout))

    def send_smtp_email(self, **kwargs: object) -> None:
        self.smtp.append((threading.current_thread().name, kwargs["timeout"]))


@pytest.mark.parametrize(("call", "expected", "socket_timeout", "sends"), CALLS)
async def test_the_event_loop_keeps_running_while_an_imap_exchange_waits(
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[EmailConfig], Coroutine[object, object, str]],
    expected: str,
    socket_timeout: int,
    sends: int,
) -> None:
    # Given an IMAP login that parks its thread until a coroutine on the event loop releases it
    del socket_timeout, sends
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()

    def park(_imap: FakeIMAP) -> None:
        _ = loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise AssertionError("the event loop did not run while the IMAP exchange waited")

    monkeypatch.setattr(email_service.imaplib, "IMAP4_SSL", partial(FakeIMAP, on_login=park))
    monkeypatch.setattr(email_service, "send_smtp_email", lambda **_kwargs: None)

    # When the call starts, this coroutine still runs during the exchange and releases it
    outcome = asyncio.create_task(call(CONFIG))
    await asyncio.wait_for(entered.wait(), 5)
    release.set()

    # Then the call finishes as it always did
    assert (await asyncio.wait_for(outcome, 5)).startswith(expected)


@pytest.mark.parametrize(("call", "expected", "socket_timeout", "sends"), CALLS)
async def test_every_exchange_runs_on_the_agent_lane_with_a_socket_timeout_and_no_shared_pool(
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[EmailConfig], Coroutine[object, object, str]],
    expected: str,
    socket_timeout: int,
    sends: int,
) -> None:
    # Given a mailbox and an SMTP send that record the thread and timeout they were given
    seen = Exchanges()
    monkeypatch.setattr(email_service.imaplib, "IMAP4_SSL", partial(FakeIMAP, on_login=seen.on_login))
    monkeypatch.setattr(email_service, "send_smtp_email", seen.send_smtp_email)

    # When the call runs where neither anyio's worker threads nor the default executor may be used
    with forbid_shared_threads(monkeypatch):
        result = await call(CONFIG)

    # Then each IMAP login and SMTP send ran on a lane thread, with a socket timeout
    assert result.startswith(expected)
    assert [(name.startswith(LANE), limit) for name, limit in seen.imap] == [(True, socket_timeout)]
    assert [(name.startswith(LANE), limit) for name, limit in seen.smtp] == [(True, socket_timeout)] * sends


@pytest.mark.parametrize(
    ("storage_type", "writers"),
    [(RecordingStorage, {"_write_base64_read", "_write_base64"}), (RangedStorage, {"_write_base64"})],
)
async def test_a_send_with_an_attachment_reads_and_sends_on_the_agent_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, storage_type: type[RecordingStorage], writers: set[str]
) -> None:
    # Given a stored attachment, and the chunk writers and the SMTP send recording the thread they ran on
    root = tmp_path / "agents"
    root.mkdir()
    storage = storage_type(str(root))
    monkeypatch.setattr("app.services.storage.get_storage_backend", lambda: storage)
    agent_id = uuid.uuid4()
    await storage.write_bytes(f"{agent_id}/workspace/a.txt", b"attached")
    ran: set[tuple[str, bool]] = set()

    def traced(name: str, function: Callable[..., object]) -> Callable[..., object]:
        def run(*args: object, **kwargs: object) -> object:
            ran.add((name, threading.current_thread().name.startswith(LANE)))
            return function(*args, **kwargs)

        return run

    for name in ("_write_base64_read", "_write_base64"):
        monkeypatch.setattr(email_service, name, traced(name, getattr(email_service, name)))
    monkeypatch.setattr(email_service, "send_smtp_stream", traced("send_smtp_stream", lambda **_kwargs: None))

    # When the agent sends it
    result = await email_service.send_email(
        CONFIG,
        "to@example.test",
        "Subject",
        "Body",
        attachments=["workspace/a.txt"],
        workspace_path=root / str(agent_id),
        agent_id=agent_id,
    )

    # Then the attachment was read and the message sent on lane threads
    assert result.startswith("\u2705")
    assert ran == {(name, True) for name in (*writers, "send_smtp_stream")}


def test_the_lane_sizes_default_and_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        monkeypatch.delenv(name)
    defaults = _settings_without_dotenv()
    assert (defaults.AGENT_EMAIL_LANE_THREADS, defaults.SYSTEM_EMAIL_LANE_THREADS) == (8, 32)

    for variable, lowest, highest in (("AGENT_EMAIL_LANE_THREADS", 1, 64), ("SYSTEM_EMAIL_LANE_THREADS", 1, 128)):
        for accepted in (lowest, highest):
            monkeypatch.setenv(variable, str(accepted))
            assert getattr(_settings_without_dotenv(), variable) == accepted
        for refused in (lowest - 1, highest + 1):
            monkeypatch.setenv(variable, str(refused))
            with pytest.raises(ValidationError):
                _ = _settings_without_dotenv()
        monkeypatch.delenv(variable)
