"""A shared turn's delivery claim follows what the bootstrap guard proved about admission, through the real wake path."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pywhales.exceptions import DockerException

from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services import openclaw_inbox, openclaw_routing, openclaw_turns
from app.services.shared_chat import shared_session_id
from guest_bootstrap_fakes import BOOTSTRAP_PENDING_EXIT, PENDING_STDERR, guard_refusal

DENIED = guard_refusal()
REFUSED = DockerException(["docker", "exec"], 1, stderr=b"ECONNREFUSED 18789")
ACTION_75 = DockerException(["docker", "exec"], BOOTSTRAP_PENDING_EXIT, stderr=b"openclaw: temporary failure")


class SharedQueue:
    """One pending shared turn behind the SQL guards of ``claim_shared`` and ``release_delivery``."""

    def __init__(self, agent: AgentRecord) -> None:
        conversation = str(shared_session_id(agent.id))
        self.row = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, conversation_id=conversation, content="human")
        self.releases: list[GatewayMessageRecord] = []

    async def get_for_agent(self, message_id: UUID, agent_id: UUID) -> GatewayMessageRecord | None:
        return self.row if (self.row.id, self.row.agent_id) == (message_id, agent_id) else None

    async def claim_shared(self, _agent_id: UUID, _session_id: UUID, *, now: datetime) -> GatewayMessageRecord | None:
        if self.row.status != "pending":
            return None
        self.row = replace(self.row, status="delivered", delivered_at=now)
        return self.row

    async def release_delivery(self, claimed: GatewayMessageRecord) -> GatewayMessageRecord | None:
        self.releases.append(claimed)
        if self.row.status != "delivered" or self.row.delivered_at != claimed.delivered_at:
            return None
        self.row = replace(self.row, status="pending", delivered_at=None)
        return self.row


class ScriptedGuestDocker:
    """Guest exec API: each call consumes the next outcome (the last repeats); ``None`` is an accepted action."""

    def __init__(self, directory: Path, *outcomes: Exception | None) -> None:
        self.container = self
        self.directory = directory
        self.outcomes = list(outcomes)
        self.calls: list[list[str]] = []
        self.admitted_keys: list[str] = []

    def execute(self, _container: str, argv: list[str], *, stream: bool = False) -> str:
        self.calls.append(argv)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if outcome is not None:
            raise outcome
        payload = json.loads((self.directory / Path(argv[-1]).name).read_text(encoding="utf-8"))
        self.admitted_keys.append(payload["idempotencyKey"])
        return ""


def guest_with(
    monkeypatch: pytest.MonkeyPatch, directory: Path, *outcomes: Exception | None
) -> tuple[AgentRecord, SharedQueue, ScriptedGuestDocker]:
    agent = AgentRecord(id=uuid4(), name="QA", creator_id=uuid4())
    queue = SharedQueue(agent)
    docker = ScriptedGuestDocker(directory, *outcomes)

    @asynccontextmanager
    async def connection() -> AsyncGenerator[None]:
        yield None

    async def open_gate(_agent_id: UUID) -> None:
        return None

    async def eligible(_agent_id: UUID) -> AgentRecord:
        return agent

    monkeypatch.setattr(openclaw_routing, "connection_ctx", connection)
    monkeypatch.setattr(openclaw_routing, "assert_work_allowed", open_gate)
    monkeypatch.setattr(openclaw_routing, "load_agent_for_new_work", eligible)
    for name in ("get_for_agent", "claim_shared", "release_delivery"):
        monkeypatch.setattr(openclaw_routing.gateway_message_dao, name, getattr(queue, name))
    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: directory)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", docker)
    monkeypatch.setattr(openclaw_inbox, "_container_ref", lambda _agent: "guest")
    return agent, queue, docker


@pytest.mark.parametrize("denial", [[DENIED], [REFUSED, DENIED]], ids=["hook-denied", "cli-fallback-denied"])
async def test_denied_shared_turn_is_released_then_dispatched_once_bootstrap_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denial: list[Exception]
) -> None:
    # Given a shared turn whose first dispatch the guard refuses, and a guest that finishes bootstrapping afterwards.
    agent, queue, docker = guest_with(monkeypatch, tmp_path, *denial, None)
    message = queue.row
    # When the wake claims the turn and the guard denies it.
    await openclaw_routing._run_inbox_wake(agent, message.content, message.id)
    # Then nothing was admitted and the exact claim is back in the queue instead of stranding it as delivered.
    assert docker.admitted_keys == []
    assert queue.row.status == "pending"
    assert queue.row.delivered_at is None
    assert len(queue.releases) == 1
    # When a later wake runs after the current boot has finished bootstrapping.
    await openclaw_routing._run_inbox_wake(agent, message.content, message.id)
    # Then the same turn is admitted exactly once and stays claimed.
    assert docker.admitted_keys == [str(message.id)]
    assert queue.row.status == "delivered"
    assert len(queue.releases) == 1


@pytest.mark.parametrize(
    "outcomes",
    [
        [DockerException(["docker", "exec"], 1, stderr=b"ReadTimeout")],
        [DockerException(["docker", "exec"], 1, stderr=b"HTTP 503")],
        [REFUSED, DockerException(["docker", "exec"], 1, stderr=b"cli crashed")],
        [RuntimeError("docker socket closed")],
        [DockerException(["docker", "exec"], BOOTSTRAP_PENDING_EXIT)],
        [ACTION_75],
        [DockerException(["docker", "exec"], BOOTSTRAP_PENDING_EXIT, stderr=b"warning\n" + PENDING_STDERR.encode())],
        [REFUSED, ACTION_75],
    ],
    ids=[
        "hook-timeout",
        "hook-5xx",
        "cli-fallback-failed",
        "transport-error",
        "hook-exit-75-silent",
        "hook-exit-75-other-stderr",
        "hook-exit-75-marker-not-first",
        "cli-fallback-exit-75",
    ],
)
async def test_ambiguous_shared_dispatch_failure_keeps_the_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcomes: list[Exception]
) -> None:
    # Given a dispatch that fails without proof that the guest never admitted the turn.
    agent, queue, docker = guest_with(monkeypatch, tmp_path, *outcomes)
    # When the wake runs.
    await openclaw_routing._run_inbox_wake(agent, queue.row.content, queue.row.id)
    # Then the claim is retained, so a possibly running turn is never requeued.
    assert docker.admitted_keys == []
    assert queue.row.status == "delivered"
    assert queue.releases == []


async def test_admitted_shared_turn_keeps_its_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, queue, docker = guest_with(monkeypatch, tmp_path, None)
    await openclaw_routing._run_inbox_wake(agent, queue.row.content, queue.row.id)
    assert docker.admitted_keys == [str(queue.row.id)]
    assert queue.row.status == "delivered"
    assert queue.releases == []


@pytest.mark.parametrize("failure", [RuntimeError("dispatch bug"), asyncio.CancelledError()], ids=["error", "cancel"])
async def test_interrupted_shared_dispatch_keeps_the_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    # Given a dispatch that is interrupted after the claim, so admission is unknown.
    agent, queue, _docker = guest_with(monkeypatch, tmp_path, None)

    async def interrupted(_agent: AgentRecord, _message: GatewayMessageRecord) -> bool:
        raise failure

    monkeypatch.setattr(openclaw_turns, "dispatch_shared_turn", interrupted)
    # When the wake runs.
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await openclaw_routing._run_inbox_wake(agent, queue.row.content, queue.row.id)
    else:
        await openclaw_routing._run_inbox_wake(agent, queue.row.content, queue.row.id)
    # Then the claim is retained.
    assert queue.row.status == "delivered"
    assert queue.releases == []


async def test_denial_release_cannot_overwrite_a_completed_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a turn that a report completes while the guard denial is still being handled.
    agent, queue, docker = guest_with(monkeypatch, tmp_path, DENIED)
    execute = docker.execute

    def complete_then_deny(container: str, argv: list[str], *, stream: bool = False) -> str:
        queue.row = replace(queue.row, status="completed")
        return execute(container, argv, stream=stream)

    monkeypatch.setattr(docker, "execute", complete_then_deny)
    # When the wake releases its claim.
    await openclaw_routing._run_inbox_wake(agent, queue.row.content, queue.row.id)
    # Then the compare-and-set refuses to rewrite the newer state.
    assert len(queue.releases) == 1
    assert queue.row.status == "completed"
