"""System email runs its SMTP sends on its own thread lane, apart from every shared pool and from agent email.

`attempt_email` (the supervised workers), `send_system_email` (request handlers) and `send_with_config` (the SMTP test of
an organization) share `SYSTEM_EMAIL_LANE`. A cancelled `attempt_email` leaves at once while its thread keeps both its
lane slot and its place in the count `wait_for_abandoned_sends` waits on.
"""

import asyncio
import threading
from collections.abc import Awaitable, Callable

import pytest

from app.core.thread_lanes import ThreadLane
from app.services import email_service, system_email_service as email
from test_email_service_lanes import CONFIG
from test_storage_thread_lanes import Probe
from thread_lane_fixtures import forbid_shared_threads

LANE = "system-email-lane"


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> email.SystemEmailConfig:
    config = email.SystemEmailConfig(
        "from@example.com", "MaraClaw", "smtp.example.com", 465, "user", "secret", True, 42
    )

    async def resolve() -> email.SystemEmailConfig:
        return config

    monkeypatch.setattr(email, "resolve_email_config_async", resolve)
    return config


async def attempt(_config: email.SystemEmailConfig) -> None:
    outcome = await email.attempt_email("to@example.com", "subject", "body", message_id="<id@example.com>")
    assert outcome.kind == "sent"


async def send_system(_config: email.SystemEmailConfig) -> None:
    await email.send_system_email("to@example.com", "subject", "body")


async def send_configured(config: email.SystemEmailConfig) -> None:
    await email.send_with_config(config, "to@example.com", "subject", "body")


@pytest.mark.parametrize("send", [attempt, send_system, send_configured])
async def test_a_system_send_runs_on_the_system_lane_and_no_shared_pool(
    monkeypatch: pytest.MonkeyPatch,
    configured: email.SystemEmailConfig,
    send: Callable[[email.SystemEmailConfig], Awaitable[None]],
) -> None:
    # Given an SMTP send that records its thread
    names: list[str] = []
    monkeypatch.setattr(email, "send_smtp_email", lambda **_kwargs: names.append(threading.current_thread().name))

    # When the message is sent where neither anyio's worker threads nor the default executor may be used
    with forbid_shared_threads(monkeypatch):
        await send(configured)

    # Then it was sent once, on a lane thread
    assert [name.startswith(LANE) for name in names] == [True]


async def test_a_busy_agent_lane_does_not_hold_up_system_mail_nor_the_reverse(
    monkeypatch: pytest.MonkeyPatch, configured: email.SystemEmailConfig
) -> None:
    # Given each lane reduced to one thread, and the agent lane's thread occupied
    agent_lane, system_lane = ThreadLane(1, name="agent-email-lane"), ThreadLane(1, name=LANE)
    monkeypatch.setattr(email_service, "AGENT_EMAIL_LANE", agent_lane)
    monkeypatch.setattr(email, "SYSTEM_EMAIL_LANE", system_lane)
    monkeypatch.setattr(email, "send_smtp_email", lambda **_kwargs: None)
    monkeypatch.setattr(email_service, "send_smtp_email", lambda **_kwargs: None)
    agent_busy, system_busy = Probe(), Probe()
    held = asyncio.create_task(agent_lane.run(agent_busy.work))
    try:
        await agent_busy.wait_entered(1)

        # When a system send runs, then it completes while the agent lane is occupied
        await asyncio.wait_for(attempt(configured), 5)
    finally:
        agent_busy.gate.set()
        await asyncio.wait_for(held, 5)

    # And the other way round: an agent send completes while the system lane's thread is occupied
    held = asyncio.create_task(system_lane.run(system_busy.work))
    try:
        await system_busy.wait_entered(1)
        result = await asyncio.wait_for(email_service.test_connection(CONFIG), 5)
    finally:
        system_busy.gate.set()
        await asyncio.wait_for(held, 5)
    assert result.get("smtp") == "\u2705 SMTP connected"


async def test_a_cancelled_attempt_leaves_at_once_but_keeps_its_thread_slot_and_its_place_in_the_restart_wait(
    monkeypatch: pytest.MonkeyPatch, configured: email.SystemEmailConfig
) -> None:
    # Given a one-thread system lane whose only send is held inside SMTP
    monkeypatch.setattr(email, "SYSTEM_EMAIL_LANE", ThreadLane(1, name=LANE))
    probe = Probe()
    monkeypatch.setattr(email, "send_smtp_email", lambda **_kwargs: probe.work())
    first = asyncio.create_task(email.attempt_email("to@example.com", "subject", "body", message_id="<a@example.com>"))
    await probe.wait_entered(1)

    # When the attempt is cancelled
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(first, 5)

    # Then another send queues behind the abandoned thread, and the restart wait still waits for it
    follower = asyncio.create_task(email.send_with_config(configured, "to@example.com", "subject", "body"))
    restart_wait = asyncio.create_task(email.wait_for_abandoned_sends())
    await asyncio.sleep(0)
    assert probe.started == 1
    assert not restart_wait.done()

    # And once the abandoned thread returns, the wait ends and the queued send runs
    probe.gate.set()
    await asyncio.wait_for(restart_wait, 5)
    await asyncio.wait_for(follower, 5)
    assert (probe.started, probe.peak) == (2, 1)
