"""Workspace mirror manifest rows and the mirror agent page against live PostgreSQL (``MARACLAW_TEST_POSTGRES_DSN``)."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from agent_deletion_fixtures import DeletionScenario, deletion_scenario
from app.dao.agent_dao import agent_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.workspace_mirror_entry_dao import workspace_mirror_entry_dao
from app.db.errors import CheckViolationError, ForeignKeyViolationError, UniqueViolationError
from app.records.workspace_mirror_entry import WorkspaceMirrorEntryRecord

__all__ = ["deletion_scenario"]

GENERATION = UUID(int=7)
UNKNOWN_AGENT = UUID(int=0)
MTIME_NS = 1_786_000_000_123_456_789


def _entry(agent_id: UUID, path: str = "workspace/notes.md") -> WorkspaceMirrorEntryRecord:
    return WorkspaceMirrorEntryRecord(
        agent_id=agent_id,
        path=path,
        generation=GENERATION,
        local_ino=4_000_000_001,
        local_size=5,
        local_mtime_ns=MTIME_NS,
        local_ctime_ns=MTIME_NS + 1,
        content_sha256="a" * 64,
        remote_etag='"etag-1"',
        remote_size=5,
        remote_token="version-1",
    )


async def _paths(agent_id: UUID) -> list[str]:
    return list(await workspace_mirror_entry_dao.list_for_agent(agent_id))


async def _page_ids(after_id: UUID | None = None, limit: int = 100) -> list[UUID]:
    return [agent.id for agent in await agent_dao.list_workspace_mirror_page(after_id, limit)]


async def test_upsert_inserts_a_row_that_list_for_agent_returns_keyed_by_path(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner")
    entry = _entry(agent_id)

    await workspace_mirror_entry_dao.upsert(entry)

    stored = await workspace_mirror_entry_dao.list_for_agent(agent_id)
    assert list(stored) == [entry.path]
    assert stored[entry.path].synced_at is not None
    assert replace(stored[entry.path], synced_at=None) == entry


async def test_a_row_without_hash_or_stored_object_round_trips_its_nulls(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner")
    entry = replace(
        _entry(agent_id),
        content_sha256=None,
        remote_etag=None,
        remote_size=None,
        remote_token=None,
        skip_reason="too_large",
    )

    await workspace_mirror_entry_dao.upsert(entry)

    stored = (await workspace_mirror_entry_dao.list_for_agent(agent_id))[entry.path]
    assert replace(stored, synced_at=None) == entry


async def test_upserting_an_existing_path_replaces_every_column_and_advances_synced_at(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner")
    first = _entry(agent_id)
    await workspace_mirror_entry_dao.upsert(first)
    await scenario.db.execute(
        "UPDATE agent_workspace_mirror_entries SET synced_at = now() - interval '1 hour' WHERE agent_id = %(id)s",
        {"id": agent_id},
    )
    before = (await workspace_mirror_entry_dao.list_for_agent(agent_id))[first.path].synced_at
    assert before is not None

    second = replace(
        first,
        generation=UUID(int=8),
        local_ino=2,
        local_size=9,
        local_mtime_ns=MTIME_NS + 10,
        local_ctime_ns=MTIME_NS + 11,
        content_sha256="b" * 64,
        remote_etag=None,
        remote_size=None,
        remote_token=None,
        skip_reason="too_large",
    )
    await workspace_mirror_entry_dao.upsert(second)

    stored = await workspace_mirror_entry_dao.list_for_agent(agent_id)
    assert list(stored) == [first.path]
    assert replace(stored[first.path], synced_at=None) == second
    after = stored[first.path].synced_at
    assert after is not None
    assert after > before


async def test_rows_are_listed_per_agent_and_per_path(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    first = await scenario.agent("owner", "First")
    second = await scenario.agent("owner", "Second")
    for agent_id, path in ((first, "memory/a.md"), (first, "workspace/b.md"), (second, "workspace/b.md")):
        await workspace_mirror_entry_dao.upsert(_entry(agent_id, path))

    assert await _paths(first) == ["memory/a.md", "workspace/b.md"]
    assert await _paths(second) == ["workspace/b.md"]
    assert await _paths(UNKNOWN_AGENT) == []


async def test_delete_paths_removes_only_the_named_rows_of_that_agent_and_counts_them(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner", "Mine")
    other = await scenario.agent("owner", "Other")
    for path in ("memory/a.md", "workspace/b.md", "workspace/c.md"):
        await workspace_mirror_entry_dao.upsert(_entry(agent_id, path))
    await workspace_mirror_entry_dao.upsert(_entry(other, "workspace/b.md"))

    assert await workspace_mirror_entry_dao.delete_paths(agent_id, ["workspace/b.md", "workspace/missing.md"]) == 1
    assert await workspace_mirror_entry_dao.delete_paths(agent_id, ["workspace/b.md"]) == 0
    assert await workspace_mirror_entry_dao.delete_paths(agent_id, []) == 0

    assert await _paths(agent_id) == ["memory/a.md", "workspace/c.md"]
    assert await _paths(other) == ["workspace/b.md"]


@pytest.mark.parametrize(
    ("known_agent", "skip_reason", "duplicate", "violation"),
    [
        (False, None, False, ForeignKeyViolationError),
        (True, "oversized", False, CheckViolationError),
        (True, None, True, UniqueViolationError),
    ],
    ids=["unknown-agent", "unknown-skip-reason", "duplicate-path"],
)
async def test_rows_belong_to_an_agent_and_keep_their_constraints(
    deletion_scenario: DeletionScenario,
    known_agent: bool,
    skip_reason: str | None,
    duplicate: bool,
    violation: type[Exception],
):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner") if known_agent else UNKNOWN_AGENT
    insert = (
        "INSERT INTO agent_workspace_mirror_entries "
        "(agent_id, path, generation, local_ino, local_size, local_mtime_ns, local_ctime_ns, skip_reason) "
        "VALUES (%(agent)s, 'workspace/a.md', %(generation)s, 1, 1, 1, 1, %(skip)s)"
    )
    params: dict[str, object] = {"agent": agent_id, "generation": uuid4(), "skip": skip_reason}
    if duplicate:
        await scenario.db.execute(insert, params)

    with pytest.raises(violation):
        await scenario.db.execute(insert, params)


async def test_deleting_an_agent_or_its_company_removes_the_rows(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner")
    kept = await scenario.agent("owner", "Kept")
    abroad = await scenario.agent("owner", "Abroad", tenant_id=scenario.foreign_tenant_id)
    for owner in (agent_id, kept, abroad):
        await workspace_mirror_entry_dao.upsert(_entry(owner))

    await agent_dao.delete_with_related(agent_id)
    assert await _paths(agent_id) == []
    assert await _paths(kept) == ["workspace/notes.md"]
    assert await _paths(abroad) == ["workspace/notes.md"]

    await tenant_dao.delete_cascade(scenario.foreign_tenant_id)
    assert await _paths(abroad) == []
    assert await _paths(kept) == ["workspace/notes.md"]


async def test_mirror_pages_cover_each_mirrored_agent_once_in_id_order_across_companies(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    mirrored = [
        await scenario.agent("owner", "Running", status="running"),
        await scenario.agent("owner", "Idle", status="idle"),
        await scenario.agent("owner", "Stopped", status="stopped"),
        await scenario.agent("owner", "Failed", status="error"),
        await scenario.agent("owner", "Abroad", tenant_id=scenario.foreign_tenant_id),
    ]
    tenantless = await scenario.agent("owner", "Tenantless")
    await scenario.db.execute("UPDATE agents SET tenant_id = NULL WHERE id = %(id)s", {"id": tenantless})
    mirrored.append(tenantless)

    seen: list[UUID] = []
    cursor: UUID | None = None
    while page := await agent_dao.list_workspace_mirror_page(cursor, 2):
        assert len(page) <= 2
        seen.extend(agent.id for agent in page)
        cursor = page[-1].id

    assert seen == sorted(mirrored)


async def test_mirror_pages_list_system_agents_and_skip_creating_and_non_openclaw_agents(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    wanted = await scenario.agent("owner", "Wanted")
    _ = await scenario.agent("owner", "Creating", status="creating")
    system = await scenario.agent("owner", "System", is_system=True)
    native = await scenario.agent("owner", "Native")
    await scenario.db.execute("UPDATE agents SET agent_type = 'native' WHERE id = %(id)s", {"id": native})

    assert await _page_ids() == sorted([wanted, system])


async def test_mirror_pages_start_after_the_cursor_and_stop_at_the_limit(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    ids = sorted([await scenario.agent("owner", f"Agent {number}") for number in range(4)])

    assert await _page_ids(limit=3) == ids[:3]
    assert await _page_ids(ids[1]) == ids[2:]
    assert await _page_ids(ids[1], 1) == [ids[2]]
    assert await _page_ids(ids[-1]) == []
