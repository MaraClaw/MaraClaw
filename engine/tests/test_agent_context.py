import asyncio
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from tests.test_redis_cache import FakeRedis

from app.core import permissions, redis_cache
from app.dao.agent_agent_relationship_dao import agent_agent_relationship_dao
from app.dao.agent_relationship_dao import agent_relationship_dao
from app.records.agent import AgentRecord
from app.records.agent_agent_relationship import AgentAgentRelationshipRecord
from app.records.agent_relationship import AgentRelationshipRecord
from app.records.org import OrgMemberRecord
from app.services import agent_context, agent_context_cache
from app.services.agent_context import _load_relationships_from_db


@pytest.mark.asyncio
async def test_build_agent_context_does_not_inject_legacy_focus_file():
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()

    async def fake_read_file(key, _max_chars=3000):
        if key == f"{agent_id}/focus.md":
            return "# Focus\n\n- [ ] follow_up: Check the deployment"
        return ""

    with (
        patch("app.services.agent_context._read_file_safe", side_effect=fake_read_file),
        patch("app.services.agent_context._load_skills_index", new_callable=AsyncMock, return_value=""),
        patch("app.services.agent_context._load_relationships_from_db", new_callable=AsyncMock, return_value=""),
        patch("app.services.timezone_utils.get_agent_timezone", new_callable=AsyncMock, return_value="UTC"),
    ):
        _static, dynamic = await build_agent_context(agent_id, "TestAgent")

    assert "## Focus" not in dynamic
    assert "follow_up: Check the deployment" not in dynamic


def _human(agent_id: uuid.UUID, name: str, provider: str | None = None) -> AgentRelationshipRecord:
    member = OrgMemberRecord(uuid.uuid4(), name, title="Lead", status="active")
    return AgentRelationshipRecord(
        uuid.uuid4(), agent_id, member.id, description=f"{name} note", member=member, provider_name=provider
    )


def _peer(agent_id: uuid.UUID, name: str) -> AgentAgentRelationshipRecord:
    target = AgentRecord(uuid.uuid4(), name, uuid.uuid4(), role_description="Analyst")
    return AgentAgentRelationshipRecord(
        uuid.uuid4(), agent_id, target.id, description=f"{name} note", target_agent=target
    )


def _statuses(batch: list[Any], restricted: set[uuid.UUID]) -> list[dict[str, Any]]:
    return [
        {
            "access_allowed": rel.id not in restricted,
            "access_status": "restricted" if rel.id in restricted else "active",
            "access_status_reason": None,
        }
        for rel in batch
    ]


async def test_load_relationships_evaluates_each_kind_in_one_batch_and_lists_only_active_rows(
    monkeypatch: pytest.MonkeyPatch,
):
    agent_id = uuid.uuid4()
    humans = [_human(agent_id, "Dana", "Feishu"), _human(agent_id, "Eli"), _human(agent_id, "Fay")]
    peers = [_peer(agent_id, "Alpha"), _peer(agent_id, "Beta")]
    restricted = {humans[1].id, peers[1].id}
    human_batches: list[list[Any]] = []
    agent_batches: list[list[Any]] = []

    async def evaluate_humans(batch: list[Any]) -> list[dict[str, Any]]:
        human_batches.append(list(batch))
        return _statuses(batch, restricted)

    async def evaluate_agents(batch: list[Any]) -> list[dict[str, Any]]:
        agent_batches.append(list(batch))
        return _statuses(batch, restricted)

    monkeypatch.setattr(
        agent_relationship_dao, "list_for_agent_with_members_and_providers", AsyncMock(return_value=humans)
    )
    monkeypatch.setattr(agent_agent_relationship_dao, "list_for_agent_with_targets", AsyncMock(return_value=peers))
    monkeypatch.setattr(permissions, "evaluate_human_relationship_statuses", evaluate_humans)
    monkeypatch.setattr(permissions, "evaluate_agent_relationship_statuses", evaluate_agents)

    text = await _load_relationships_from_db(agent_id)

    assert human_batches == [humans]
    assert agent_batches == [peers]
    assert "### Dana - Lead (synced through Feishu)" in text
    assert "- Dana note" in text
    assert text.index("### Dana") < text.index("### Fay - Lead")
    assert "### Alpha - Analyst" in text
    assert "Eli" not in text
    assert "Beta" not in text


async def test_load_relationships_is_empty_when_no_row_is_active(monkeypatch: pytest.MonkeyPatch):
    agent_id = uuid.uuid4()
    human, peer = _human(agent_id, "Dana"), _peer(agent_id, "Alpha")
    monkeypatch.setattr(
        agent_relationship_dao, "list_for_agent_with_members_and_providers", AsyncMock(return_value=[human])
    )
    monkeypatch.setattr(agent_agent_relationship_dao, "list_for_agent_with_targets", AsyncMock(return_value=[peer]))
    monkeypatch.setattr(
        permissions,
        "evaluate_human_relationship_statuses",
        AsyncMock(side_effect=lambda batch: _statuses(batch, {human.id})),
    )
    monkeypatch.setattr(
        permissions,
        "evaluate_agent_relationship_statuses",
        AsyncMock(side_effect=lambda batch: _statuses(batch, {peer.id})),
    )

    assert await _load_relationships_from_db(agent_id) == ""


class _Storage:
    """Workspace storage stand-in: counts soul reads and lets a test hold the first one open."""

    def __init__(self) -> None:
        self.soul_reads = 0
        self.gate: asyncio.Event | None = None
        self.failures: list[Exception] = []

    async def read(self, key: str, max_chars: int = 3000) -> str:
        del max_chars
        if not key.endswith("soul.md"):
            return ""
        self.soul_reads += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.failures:
            raise self.failures.pop(0)
        return "soul body"


@pytest.fixture
def context_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FakeRedis, _Storage]]:
    redis_cache.reset_circuit()
    cache, storage = FakeRedis(), _Storage()

    async def client() -> FakeRedis:
        return cache

    async def no_skills(_agent_id: object) -> str:
        return ""

    async def no_relationships(_agent_id: object) -> str:
        return ""

    async def utc(_agent_id: object) -> str:
        return "UTC"

    monkeypatch.setattr(agent_context_cache, "get_cache_redis", client)
    monkeypatch.setattr(redis_cache, "_client", client)
    monkeypatch.setattr(agent_context_cache, "_ttl", lambda: 60)
    monkeypatch.setattr(agent_context, "_read_file_safe", storage.read)
    monkeypatch.setattr(agent_context, "_load_skills_index", no_skills)
    monkeypatch.setattr(agent_context, "_load_relationships_from_db", no_relationships)
    monkeypatch.setattr("app.services.timezone_utils.get_agent_timezone", utc)
    yield cache, storage
    redis_cache.reset_circuit()


async def _build(agent_id: uuid.UUID) -> str:
    static, dynamic = await agent_context.build_agent_context(agent_id, "Name", "role")
    return static + dynamic


async def test_a_turn_reads_all_three_kinds_in_one_cache_round_trip(context_env: tuple[FakeRedis, _Storage]) -> None:
    cache, _ = context_env
    agent_id = uuid.uuid4()

    await _build(agent_id)
    assert cache.round_trips == 1

    await _build(agent_id)
    assert cache.round_trips == 2


async def test_a_dead_cache_costs_the_first_turn_one_timeout_and_later_turns_none(
    context_env: tuple[FakeRedis, _Storage],
) -> None:
    cache, storage = context_env
    cache.fail = True

    turns = [await _build(uuid.uuid4()) for _ in range(3)]

    assert all("soul body" in turn for turn in turns)
    assert cache.round_trips == 1
    assert storage.soul_reads == 3


async def test_concurrent_misses_share_one_storage_load_and_one_fill(context_env: tuple[FakeRedis, _Storage]) -> None:
    cache, storage = context_env
    storage.gate = asyncio.Event()
    agent_id = uuid.uuid4()

    turns = [asyncio.create_task(_build(agent_id)) for _ in range(3)]
    await asyncio.sleep(0)
    storage.gate.set()

    assert len(set(await asyncio.gather(*turns))) == 1
    assert storage.soul_reads == 1
    assert [key for key in cache.store if key.startswith(f"ctx:{agent_id}:")] == [
        f"ctx:{agent_id}:soul",
        f"ctx:{agent_id}:memory",
        f"ctx:{agent_id}:skills",
    ]


async def test_a_failed_shared_load_reaches_every_turn_and_the_next_turn_retries(
    context_env: tuple[FakeRedis, _Storage],
) -> None:
    _, storage = context_env
    storage.gate = asyncio.Event()
    storage.failures.append(OSError("storage down"))
    agent_id = uuid.uuid4()

    turns = [asyncio.create_task(_build(agent_id)) for _ in range(2)]
    await asyncio.sleep(0)
    storage.gate.set()
    outcomes = await asyncio.gather(*turns, return_exceptions=True)

    assert [type(outcome) for outcome in outcomes] == [OSError, OSError]
    assert "soul body" in await _build(agent_id)
    assert storage.soul_reads == 2


async def test_a_turn_after_an_invalidation_never_joins_a_load_that_started_before_it(
    context_env: tuple[FakeRedis, _Storage],
) -> None:
    _, storage = context_env
    storage.gate = asyncio.Event()
    agent_id = uuid.uuid4()

    before = asyncio.create_task(_build(agent_id))
    await asyncio.sleep(0)
    await agent_context_cache.invalidate_for_workspace_paths(agent_id, "soul.md")
    after = asyncio.create_task(_build(agent_id))
    await asyncio.sleep(0)
    storage.gate.set()
    await asyncio.gather(before, after)

    assert storage.soul_reads == 2


async def test_a_cancelled_turn_does_not_cancel_the_load_the_others_wait_on(
    context_env: tuple[FakeRedis, _Storage],
) -> None:
    _, storage = context_env
    storage.gate = asyncio.Event()
    agent_id = uuid.uuid4()

    first = asyncio.create_task(_build(agent_id))
    second = asyncio.create_task(_build(agent_id))
    await asyncio.sleep(0)
    _ = first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    storage.gate.set()

    assert "soul body" in await second
    assert storage.soul_reads == 1
