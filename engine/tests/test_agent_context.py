import asyncio
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from tests.bounded_read_fakes import RecordingStorage
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
from app.services.storage_runtime.text_head import TRUNCATED_MARKER, head_bytes_for_chars


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


def _workspace(monkeypatch: pytest.MonkeyPatch, files: dict[str, bytes]) -> RecordingStorage:
    storage = RecordingStorage(files)
    monkeypatch.setattr(agent_context, "get_storage_backend", lambda: storage)
    return storage


async def test_read_file_safe_returns_a_small_file_stripped_from_one_head_read(monkeypatch: pytest.MonkeyPatch):
    storage = _workspace(monkeypatch, {"a/soul.md": "\n  # Soul \u20ac\n".encode()})

    assert await agent_context._read_file_safe("a/soul.md") == "# Soul \u20ac"
    assert storage.reads == [("a/soul.md", head_bytes_for_chars(3000), 0)]
    assert storage.whole_reads == []


@pytest.mark.parametrize(("chars", "marked"), [(3000, False), (3001, True)])
async def test_read_file_safe_marks_only_a_file_over_the_cap(monkeypatch: pytest.MonkeyPatch, chars: int, marked: bool):
    _workspace(monkeypatch, {"a/memory.md": b"m" * chars})

    expected = "m" * 3000 + (TRUNCATED_MARKER if marked else "")
    assert await agent_context._read_file_safe("a/memory.md") == expected


async def test_read_file_safe_reads_only_the_head_of_a_huge_file(monkeypatch: pytest.MonkeyPatch):
    storage = _workspace(monkeypatch, {"a/soul.md": b"s" * 1_000_000})

    assert await agent_context._read_file_safe("a/soul.md", 30000) == "s" * 30000 + TRUNCATED_MARKER
    assert storage.reads == [("a/soul.md", head_bytes_for_chars(30000), 0)]
    assert storage.whole_reads == []


async def test_read_file_safe_drops_a_character_cut_at_the_end_of_the_head(monkeypatch: pytest.MonkeyPatch):
    padding = head_bytes_for_chars(3000) - 4
    _workspace(monkeypatch, {"a/soul.md": b" " * padding + "\u20ac".encode() * 5})

    assert await agent_context._read_file_safe("a/soul.md") == "\u20ac" + TRUNCATED_MARKER


async def test_read_file_safe_is_empty_for_a_missing_file_a_folder_and_a_failed_read(monkeypatch: pytest.MonkeyPatch):
    storage = _workspace(monkeypatch, {"a/dir/inner.md": b"x"})

    assert await agent_context._read_file_safe("a/missing.md") == ""
    assert await agent_context._read_file_safe("a/dir") == ""
    monkeypatch.setattr(storage, "read_bytes_bounded", AsyncMock(side_effect=OSError("down")))
    assert await agent_context._read_file_safe("a/dir/inner.md") == ""


SKILL = "---\nname: reporting\ndescription: G\u00e9n\u00e9rer des rapports\n---\n# Reporting\n"
SKILL_ROW = "| reporting | G\u00e9n\u00e9rer des rapports | skills/reporting/SKILL.md |"


async def test_skills_index_reads_one_head_per_skill_file(monkeypatch: pytest.MonkeyPatch):
    agent_id = uuid.uuid4()
    prefix = f"{agent_id}/skills"
    files = {
        f"{prefix}/reporting/SKILL.md": SKILL.encode(),
        f"{prefix}/lower/skill.md": b"---\nname: lower\ndescription: lowercase file\n---\n",
        f"{prefix}/flat.md": b"# Flat\nFlat skill description\n",
        f"{prefix}/notes.txt": b"not a skill",
        f"{prefix}/.hidden.md": b"hidden",
    }
    storage = _workspace(monkeypatch, files)

    index = await agent_context._load_skills_index(agent_id)

    assert SKILL_ROW in index
    assert "| lower | lowercase file | skills/lower/SKILL.md |" in index
    assert "| flat | Flat skill description | skills/flat.md |" in index
    assert "notes" not in index
    assert "hidden" not in index
    head = agent_context.SKILL_HEAD_BYTES
    assert sorted(storage.reads) == sorted(
        [
            (f"{prefix}/reporting/SKILL.md", head, 0),
            (f"{prefix}/lower/skill.md", head, 0),
            (f"{prefix}/flat.md", head, 0),
        ]
    )
    assert storage.whole_reads == []


async def test_skills_index_row_of_a_huge_skill_comes_from_its_head(monkeypatch: pytest.MonkeyPatch):
    agent_id = uuid.uuid4()
    key = f"{agent_id}/skills/reporting/SKILL.md"
    storage = _workspace(monkeypatch, {key: SKILL.encode() + b"body\n" * 200_000})

    index = await agent_context._load_skills_index(agent_id)

    assert SKILL_ROW in index
    assert storage.reads == [(key, agent_context.SKILL_HEAD_BYTES, 0)]


async def test_skills_index_reads_frontmatter_longer_than_the_head_like_one_without_a_closing_marker(
    monkeypatch: pytest.MonkeyPatch,
):
    agent_id = uuid.uuid4()
    long_frontmatter = "---\nname: custom\n" + "extra: value\n" * agent_context.SKILL_HEAD_BYTES + "---\n# Title\n"
    _workspace(monkeypatch, {f"{agent_id}/skills/bigskill/SKILL.md": long_frontmatter.encode()})

    index = await agent_context._load_skills_index(agent_id)

    assert "| bigskill | extra: value | skills/bigskill/SKILL.md |" in index


async def test_skills_index_drops_a_character_cut_at_the_end_of_the_head(monkeypatch: pytest.MonkeyPatch):
    agent_id = uuid.uuid4()
    head = agent_context.SKILL_HEAD_BYTES
    _workspace(monkeypatch, {f"{agent_id}/skills/cut.md": b"\n" * (head - 1) + "\u20ac".encode() + b"tail"})

    index = await agent_context._load_skills_index(agent_id)

    assert "| cut |  | skills/cut.md |" in index
