import hashlib
import os
from collections.abc import AsyncIterator, Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, override
from uuid import UUID, uuid4

import pytest
from anyio import fail_after, wait_readable
from anyio.lowlevel import checkpoint
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.config import get_settings
from app.core.json_types import JsonObject, JsonValue
from app.db.connection import DbConnection
from app.db.pool import close_pool, init_pool
from app.records.skill import SkillFileRecord, SkillRecord
from app.scripts import bootstrap_db
from app.services import skill_seeder, storage
from app.services.agent_manager import agent_manager
from app.services.skill_provider import parse_skill_md_frontmatter
from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    StorageReadLimitExceededError,
    StorageSnapshot,
    StorageVersion,
    WriteCondition,
)
from app.services.storage_runtime.quota_errors import StorageQuotaExceededError

SETTING_KEY = "default_skills_sync_hash"
SKILL_MD = "current-default/SKILL.md"
RUN_PY = "current-default/scripts/run.py"
CURRENT = {SKILL_MD: "current instructions", RUN_PY: "print('current')"}
PREVIOUS = {SKILL_MD: "previous instructions", RUN_PY: "print('previous')"}


def sha256_hex(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def skill_key(agent_id: UUID, relative_path: str) -> str:
    return f"{agent_manager._agent_storage_prefix(agent_id)}/skills/{relative_path}"


@dataclass
class MemoryStorage:
    files: dict[str, str] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)
    inspected: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    # Runs after a read has been answered and before it returns: a writer that lands between read and write.
    after_read: Callable[[str], None] | None = None

    def edit(self, key: str, content: str) -> None:
        """A write by someone else (the guest or its owner): replaces the content and advances the version."""
        self.files[key] = content
        self.versions[key] = self.versions.get(key, 0) + 1

    def version_of(self, key: str) -> StorageVersion:
        content = self.files.get(key)
        size = 0 if content is None else len(content.encode("utf-8"))
        return StorageVersion(
            key=key, exists=content is not None, is_dir=False, size=size, etag=f"v{self.versions.get(key, 0)}"
        )

    async def read_snapshot_bounded(self, key: str, max_bytes: int) -> StorageSnapshot:
        self.inspected.append(key)
        content = self.files.get(key)
        snapshot: StorageSnapshot | None = None
        if content is not None:
            data = content.encode("utf-8")
            if len(data) > max_bytes:
                raise StorageReadLimitExceededError(max_bytes, len(data))
            snapshot = StorageSnapshot(data=data, version=self.version_of(key), sha256=hashlib.sha256(data).hexdigest())
        if self.after_read is not None:
            self.after_read(key)
        if snapshot is None:
            raise FileNotFoundError(key)
        return snapshot

    async def create_bytes_if_absent(
        self, key: str, data: bytes, content_type: str | None = None
    ) -> ConditionalWriteResult:
        if key in self.files:
            return ConditionalWriteResult(ok=False, conflict=True)
        self.written.append(key)
        self.edit(key, data.decode("utf-8"))
        return ConditionalWriteResult(ok=True, current_version=self.version_of(key))

    async def write_bytes_if_match(
        self,
        key: str,
        data: bytes,
        *,
        condition: WriteCondition | None = None,
        content_type: str | None = None,
    ) -> ConditionalWriteResult:
        current = self.version_of(key)
        if condition is not None and condition.version_token is not None and condition.version_token != current.token:
            return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
        self.written.append(key)
        self.edit(key, data.decode("utf-8"))
        return ConditionalWriteResult(ok=True, current_version=self.version_of(key))

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        del self.files[key]


@dataclass
class QuotaFullStorage(MemoryStorage):
    blocked_prefix: str | None = None

    def refuse_when_blocked(self, key: str, data: bytes) -> None:
        if self.blocked_prefix is not None and key.startswith(self.blocked_prefix):
            raise StorageQuotaExceededError(limit_bytes=1, charged_bytes=1, size_bytes=len(data))

    @override
    async def create_bytes_if_absent(
        self, key: str, data: bytes, content_type: str | None = None
    ) -> ConditionalWriteResult:
        self.refuse_when_blocked(key, data)
        return await super().create_bytes_if_absent(key, data, content_type)

    @override
    async def write_bytes_if_match(
        self,
        key: str,
        data: bytes,
        *,
        condition: WriteCondition | None = None,
        content_type: str | None = None,
    ) -> ConditionalWriteResult:
        self.refuse_when_blocked(key, data)
        return await super().write_bytes_if_match(key, data, condition=condition, content_type=content_type)


@dataclass
class ConcurrencyStorage(MemoryStorage):
    """Yields on every read so agents that sync at once overlap, and records how many did."""

    inflight: int = 0
    peak: int = 0

    @override
    async def read_snapshot_bounded(self, key: str, max_bytes: int) -> StorageSnapshot:
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await checkpoint()
            return await super().read_snapshot_bounded(key, max_bytes)
        finally:
            self.inflight -= 1


@dataclass
class SyncSetting:
    value: JsonValue


@dataclass
class Reconciliation:
    defaults: list[SkillRecord]
    agent_ids: list[UUID]
    storage: MemoryStorage = field(default_factory=MemoryStorage)
    setting: SyncSetting | None = None
    persisted: list[JsonObject] = field(default_factory=list)
    setting_reads: int = 0
    agent_reads: int = 0
    storage_reads: int = 0

    async def list_defaults(self) -> list[SkillRecord]:
        return self.defaults

    async def get_setting(self, key: str) -> SyncSetting | None:
        assert key == SETTING_KEY
        self.setting_reads += 1
        return self.setting

    async def list_ids_page(self, *, after_id: UUID | None, limit: int) -> list[UUID]:
        self.agent_reads += 1
        return sorted(agent_id for agent_id in self.agent_ids if after_id is None or agent_id > after_id)[:limit]

    def get_storage(self) -> MemoryStorage:
        self.storage_reads += 1
        return self.storage

    async def create_setting(self, *, obj_in: JsonObject) -> None:
        self.persisted.append(obj_in)

    async def update_setting(self, *, db_obj: SyncSetting, obj_in: JsonObject) -> None:
        assert db_obj is self.setting
        self.persisted.append(obj_in)


@pytest.fixture
def reconciliation(monkeypatch: pytest.MonkeyPatch) -> Reconciliation:
    skill_id = uuid4()
    skill = SkillRecord(
        id=skill_id,
        name="Current default",
        folder_name="current-default",
        is_default=True,
        files=[
            SkillFileRecord(id=uuid4(), skill_id=skill_id, path="SKILL.md", content=CURRENT[SKILL_MD]),
            SkillFileRecord(id=uuid4(), skill_id=skill_id, path="scripts/run.py", content=CURRENT[RUN_PY]),
        ],
    )
    state = Reconciliation(defaults=[skill], agent_ids=[uuid4(), uuid4()])
    monkeypatch.setattr(skill_seeder, "connection_ctx", nullcontext)
    monkeypatch.setattr(skill_seeder.skill_dao, "list_defaults_with_files", state.list_defaults)
    monkeypatch.setattr(skill_seeder.system_setting_dao, "get_by_key", state.get_setting)
    monkeypatch.setattr(skill_seeder.system_setting_dao, "create", state.create_setting)
    monkeypatch.setattr(skill_seeder.system_setting_dao, "update", state.update_setting)
    monkeypatch.setattr(skill_seeder.agent_dao, "list_ids_page", state.list_ids_page)
    monkeypatch.setattr(storage, "get_storage_backend", state.get_storage)
    return state


def current_manifest(state: Reconciliation) -> dict[str, str]:
    """The checksum of every default file, keyed by folder and path, as a start records it."""
    return {
        f"{skill.folder_name}/{skill_file.path}": sha256_hex(skill_file.content)
        for skill in state.defaults
        for skill_file in skill.files
    }


def previous_push(contents: dict[str, str]) -> SyncSetting:
    """The setting a start left behind after pushing these file contents."""
    return SyncSetting(
        value={"hash": "previous-push", "files": {path: sha256_hex(content) for path, content in contents.items()}}
    )


def recorded(state: Reconciliation) -> JsonObject:
    """The one setting value the run persisted."""
    (entry,) = state.persisted
    value = entry["value"]
    assert isinstance(value, dict)
    return value


async def record_current_defaults(state: Reconciliation) -> JsonObject:
    """What a start records for the current defaults, taken by running it against no agents."""
    agent_ids, state.agent_ids = state.agent_ids, []
    await skill_seeder.push_default_skills_to_existing_agents()
    value = recorded(state)
    state.agent_ids = agent_ids
    state.persisted.clear()
    state.setting_reads = state.agent_reads = state.storage_reads = 0
    return value


def hold(state: Reconciliation, agent_id: UUID, contents: dict[str, str]) -> None:
    state.storage.files.update({skill_key(agent_id, path): content for path, content in contents.items()})


def expect_files(state: Reconciliation, expected: dict[UUID, dict[str, str]]) -> None:
    assert state.storage.files == {
        skill_key(agent_id, path): content
        for agent_id, contents in expected.items()
        for path, content in contents.items()
    }


@pytest.mark.parametrize("existing_setting", [False, True])
async def test_reconciliation_leaves_obsolete_file_untouched_when_sync_is_required(
    reconciliation: Reconciliation, existing_setting: bool
) -> None:
    # Given: stale or absent hash forces the real reconciliation with obsolete files present.
    if existing_setting:
        reconciliation.setting = SyncSetting(value={"hash": "stale"})
    obsolete_keys = [
        f"{agent_manager._agent_storage_prefix(agent_id)}/skills/MCP_INSTALLER.md"
        for agent_id in reconciliation.agent_ids
    ]
    reconciliation.storage.files.update(dict.fromkeys(obsolete_keys, "user-owned obsolete content"))
    expected_files = {
        f"{agent_manager._agent_storage_prefix(agent_id)}/skills/{skill.folder_name}/{skill_file.path}": skill_file.content
        for agent_id in reconciliation.agent_ids
        for skill in reconciliation.defaults
        for skill_file in skill.files
    }

    # When: the actual startup reconciliation runs.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then: obsolete paths never cross the storage boundary, current files and their checksums are persisted.
    assert set(obsolete_keys).isdisjoint(reconciliation.storage.inspected)
    assert reconciliation.storage.deleted == []
    assert reconciliation.storage.files == {
        **dict.fromkeys(obsolete_keys, "user-owned obsolete content"),
        **expected_files,
    }
    assert set(reconciliation.storage.written) == set(expected_files)
    assert len(reconciliation.persisted) == 1
    assert reconciliation.persisted[0].get("key") == (None if existing_setting else SETTING_KEY)
    value = recorded(reconciliation)
    assert value["files"] == current_manifest(reconciliation)
    assert isinstance(value["hash"], str)


async def test_reconciliation_skips_agents_and_storage_when_hash_is_unchanged(reconciliation: Reconciliation) -> None:
    # Given: the setting holds what the previous start recorded for the current defaults.
    reconciliation.setting = SyncSetting(value=await record_current_defaults(reconciliation))

    # When.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then.
    assert reconciliation.agent_reads == reconciliation.storage_reads == 0
    assert reconciliation.persisted == []


@pytest.mark.parametrize("change", ["content", "path", "folder"])
async def test_recorded_hash_covers_folder_path_and_content_of_every_default_file(
    reconciliation: Reconciliation, change: str
) -> None:
    # Given: the setting holds what the previous start recorded.
    reconciliation.setting = SyncSetting(value=await record_current_defaults(reconciliation))
    skill = reconciliation.defaults[0]

    # When: one default file changes in its content, its path, or its folder.
    if change == "content":
        skill.files[1].content = "print('changed')"
    elif change == "path":
        skill.files[1].path = "scripts/renamed.py"
    else:
        skill.folder_name = "renamed-default"
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then: the start no longer matches the record, so it reconciles and records the new checksums.
    assert reconciliation.agent_reads == 1
    assert recorded(reconciliation)["files"] == current_manifest(reconciliation)


async def test_content_change_overwrites_unmodified_copies_and_creates_missing_files(
    reconciliation: Reconciliation,
) -> None:
    # Given: one agent holds what was pushed last time, another holds only the first file of it.
    whole, partial = reconciliation.agent_ids
    reconciliation.setting = previous_push(PREVIOUS)
    hold(reconciliation, whole, PREVIOUS)
    hold(reconciliation, partial, {SKILL_MD: PREVIOUS[SKILL_MD]})

    # When.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then: unmodified copies carry the new content, the missing file is created, the checksums are recorded.
    expect_files(reconciliation, {whole: CURRENT, partial: CURRENT})
    assert sorted(reconciliation.storage.written) == sorted(
        skill_key(agent_id, path) for agent_id in (whole, partial) for path in CURRENT
    )
    assert recorded(reconciliation)["files"] == current_manifest(reconciliation)


async def test_content_change_keeps_a_copy_the_owner_edited(
    reconciliation: Reconciliation, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: one agent's SKILL.md was edited since the last push; its other file is still as pushed.
    pristine, edited = reconciliation.agent_ids
    reconciliation.setting = previous_push(PREVIOUS)
    hold(reconciliation, pristine, PREVIOUS)
    hold(reconciliation, edited, {**PREVIOUS, SKILL_MD: "owner edit"})
    infos: list[str] = []
    monkeypatch.setattr(skill_seeder.logger, "info", lambda message, *_args: infos.append(str(message)))

    # When.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then: the edit survives, every other file is updated, the kept file is logged, and the run still records.
    expect_files(reconciliation, {pristine: CURRENT, edited: {**CURRENT, SKILL_MD: "owner edit"}})
    assert skill_key(edited, SKILL_MD) not in reconciliation.storage.written
    assert any(str(edited) in message and SKILL_MD in message for message in infos)
    assert recorded(reconciliation)["files"] == current_manifest(reconciliation)


async def test_edit_landing_between_read_and_write_wins(
    reconciliation: Reconciliation, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: an unmodified copy whose owner edits it after the start read it and before the start writes.
    agent_id = reconciliation.agent_ids[0]
    reconciliation.agent_ids = [agent_id]
    reconciliation.setting = previous_push(PREVIOUS)
    hold(reconciliation, agent_id, PREVIOUS)
    store = reconciliation.storage
    racing_key = skill_key(agent_id, SKILL_MD)

    def owner_edits(key: str) -> None:
        if key == racing_key:
            store.after_read = None
            store.edit(key, "edit landing after the read")

    store.after_read = owner_edits
    infos: list[str] = []
    monkeypatch.setattr(skill_seeder.logger, "info", lambda message, *_args: infos.append(str(message)))

    # When.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then: the conditional write conflicts, the edit is kept and logged, and the other file is still updated.
    assert store.files[racing_key] == "edit landing after the read"
    assert racing_key not in store.written
    assert store.files[skill_key(agent_id, RUN_PY)] == CURRENT[RUN_PY]
    assert any(str(agent_id) in message and SKILL_MD in message for message in infos)
    assert recorded(reconciliation)["files"] == current_manifest(reconciliation)


async def test_file_created_by_the_owner_before_the_write_is_kept(reconciliation: Reconciliation) -> None:
    # Given: a missing file that its owner creates after the start found it absent.
    agent_id = reconciliation.agent_ids[0]
    reconciliation.agent_ids = [agent_id]
    store = reconciliation.storage
    racing_key = skill_key(agent_id, SKILL_MD)

    def owner_creates(key: str) -> None:
        if key == racing_key:
            store.after_read = None
            store.edit(key, "file the owner created")

    store.after_read = owner_creates

    # When.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then: creation never replaces it.
    assert store.files[racing_key] == "file the owner created"
    assert racing_key not in store.written
    assert store.files[skill_key(agent_id, RUN_PY)] == CURRENT[RUN_PY]


@pytest.mark.parametrize("legacy", [{"hash": "legacy"}, "legacy"])
async def test_legacy_setting_keeps_existing_copies_but_writes_missing_files(
    reconciliation: Reconciliation, legacy: JsonValue
) -> None:
    # Given: the setting predates the per-file record (a bare hash, or a plain string).
    reconciliation.setting = SyncSetting(value=legacy)
    holder, empty = reconciliation.agent_ids
    hold(reconciliation, holder, {SKILL_MD: PREVIOUS[SKILL_MD]})

    # When.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then: no previous push is known, so the existing copy stays; missing files are written; the record is created.
    expect_files(reconciliation, {holder: {SKILL_MD: PREVIOUS[SKILL_MD], RUN_PY: CURRENT[RUN_PY]}, empty: CURRENT})
    assert skill_key(holder, SKILL_MD) not in reconciliation.storage.written
    assert recorded(reconciliation)["files"] == current_manifest(reconciliation)


async def test_copy_already_at_the_new_content_is_not_rewritten(reconciliation: Reconciliation) -> None:
    # Given: copies that already hold the new content, with a record of an older push.
    reconciliation.setting = previous_push(PREVIOUS)
    for agent_id in reconciliation.agent_ids:
        hold(reconciliation, agent_id, CURRENT)

    # When.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then.
    assert reconciliation.storage.written == []
    assert recorded(reconciliation)["files"] == current_manifest(reconciliation)


async def test_oversized_copy_is_kept_and_does_not_fail_the_agent(reconciliation: Reconciliation) -> None:
    # Given: a copy far larger than any default file, so it cannot be what was pushed.
    agent_id = reconciliation.agent_ids[0]
    reconciliation.agent_ids = [agent_id]
    reconciliation.setting = previous_push(PREVIOUS)
    oversized = "x" * (16 * 1024 * 1024)
    hold(reconciliation, agent_id, {SKILL_MD: oversized, RUN_PY: PREVIOUS[RUN_PY]})

    # When.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then: it is kept like any edited copy, and the run still records.
    expect_files(reconciliation, {agent_id: {SKILL_MD: oversized, RUN_PY: CURRENT[RUN_PY]}})
    assert recorded(reconciliation)["files"] == current_manifest(reconciliation)


@pytest.mark.parametrize("record", ["previous-push", "legacy"])
async def test_content_only_refresh_preserves_existing_default_workspace(
    reconciliation: Reconciliation, record: str
) -> None:
    # Given: customized copies, with either the previous push recorded or only a legacy setting.
    if record == "previous-push":
        reconciliation.setting = SyncSetting(value=await record_current_defaults(reconciliation))
    else:
        reconciliation.setting = SyncSetting(value={"hash": "old"})
    existing = {
        f"{agent_manager._agent_storage_prefix(agent_id)}/skills/current-default/{file.path}": "workspace customization"
        for agent_id in reconciliation.agent_ids
        for file in reconciliation.defaults[0].files
    }
    reconciliation.storage.files.update(existing)
    reconciliation.defaults[0].files[0].content = "refreshed registry instructions"

    # When.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then: the refresh ran and left every customized copy alone.
    assert reconciliation.agent_reads == 1
    assert reconciliation.storage.files == existing
    assert reconciliation.storage.written == reconciliation.storage.deleted == []


async def test_reconciliation_returns_before_hash_lookup_when_defaults_are_empty(
    reconciliation: Reconciliation,
) -> None:
    # Given.
    reconciliation.defaults.clear()

    # When.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then.
    assert reconciliation.setting_reads == reconciliation.agent_reads == reconciliation.storage_reads == 0
    assert reconciliation.persisted == []


async def test_reconciliation_keeps_existing_first_file_and_backfills_missing_secondary_files(
    reconciliation: Reconciliation,
) -> None:
    # Given: existing first files keep their content; their missing secondary files are written.
    for agent_id in reconciliation.agent_ids:
        hold(reconciliation, agent_id, {SKILL_MD: "customized instructions"})
    reconciliation.defaults.append(SkillRecord(id=uuid4(), name="Empty", folder_name="empty", is_default=True))

    # When.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then: a skill without files contributes nothing to the record.
    expect_files(
        reconciliation,
        {
            agent_id: {SKILL_MD: "customized instructions", RUN_PY: CURRENT[RUN_PY]}
            for agent_id in reconciliation.agent_ids
        },
    )
    assert sorted(reconciliation.storage.written) == sorted(
        skill_key(agent_id, RUN_PY) for agent_id in reconciliation.agent_ids
    )
    assert recorded(reconciliation)["files"] == current_manifest(reconciliation)


async def test_reconciliation_isolates_a_failing_agent_and_retries_it_on_the_next_start(
    reconciliation: Reconciliation, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: the first agent's storage is full while the second agent's is not.
    blocked, healthy = reconciliation.agent_ids
    storage = QuotaFullStorage(blocked_prefix=agent_manager._agent_storage_prefix(blocked))
    reconciliation.storage = storage
    warnings: list[str] = []
    monkeypatch.setattr(skill_seeder.logger, "warning", lambda message, *_args: warnings.append(str(message)))

    def skill_files(agent_id: UUID) -> dict[str, str]:
        return {
            f"{agent_manager._agent_storage_prefix(agent_id)}/skills/{skill.folder_name}/{skill_file.path}": skill_file.content
            for skill in reconciliation.defaults
            for skill_file in skill.files
        }

    # When: the reconciliation runs.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then: the healthy agent still received the skill, the failure was logged and the hash was not recorded.
    assert storage.files == skill_files(healthy)
    assert reconciliation.persisted == []
    assert any(str(blocked) in message and "StorageQuotaExceededError" in message for message in warnings)

    # When: the next startup finds the agent's storage usable again.
    storage.blocked_prefix = None
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then: the retried agent gets the skill and the record is persisted.
    assert storage.files == {**skill_files(blocked), **skill_files(healthy)}
    assert len(reconciliation.persisted) == 1
    assert recorded(reconciliation)["files"] == current_manifest(reconciliation)


async def test_failed_overwrite_keeps_the_previous_record_and_is_retried(
    reconciliation: Reconciliation, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: both agents hold the last push, and the first agent's storage refuses the overwrite.
    blocked, healthy = reconciliation.agent_ids
    store = QuotaFullStorage(blocked_prefix=agent_manager._agent_storage_prefix(blocked))
    reconciliation.storage = store
    reconciliation.setting = previous_push(PREVIOUS)
    for agent_id in reconciliation.agent_ids:
        hold(reconciliation, agent_id, PREVIOUS)
    monkeypatch.setattr(skill_seeder.logger, "warning", lambda message, *_args: None)

    # When: the first start runs.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then: only the healthy agent moved on and the previous record stays.
    expect_files(reconciliation, {blocked: PREVIOUS, healthy: CURRENT})
    assert reconciliation.persisted == []

    # When: the next start finds the storage usable again.
    store.blocked_prefix = None
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then: the blocked agent is overwritten, the healthy one is not rewritten, and the record is persisted.
    expect_files(reconciliation, {blocked: CURRENT, healthy: CURRENT})
    assert sorted(store.written) == sorted(
        skill_key(agent_id, path) for agent_id in (blocked, healthy) for path in CURRENT
    )
    assert recorded(reconciliation)["files"] == current_manifest(reconciliation)


async def test_the_sweep_pages_agents_by_key_and_bounds_the_agents_synced_at_once(
    reconciliation: Reconciliation, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: five agents, pages of two, at most two agents in flight, and a storage that yields on every read.
    monkeypatch.setattr(skill_seeder, "_SWEEP_PAGE_SIZE", 2)
    monkeypatch.setattr(skill_seeder, "_SWEEP_CONCURRENCY", 2)
    reconciliation.agent_ids = [uuid4() for _ in range(5)]
    store = ConcurrencyStorage()
    reconciliation.storage = store

    # When: the reconciliation runs.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then: the ids came in three keyset reads, every agent has the skill, no more than two synced at once, and the
    # record is persisted.
    assert reconciliation.agent_reads == 3
    assert store.peak == 2
    expect_files(reconciliation, dict.fromkeys(reconciliation.agent_ids, CURRENT))
    assert recorded(reconciliation)["files"] == current_manifest(reconciliation)


async def test_seed_registers_template_skills_as_defaults_with_their_skill_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upserts: dict[str, dict[str, Any]] = {}

    async def no_existing_skill(folder_name: str) -> None:
        return None

    async def record_upsert(**fields: Any) -> None:
        upserts[fields["folder_name"]] = fields

    monkeypatch.setattr(skill_seeder, "connection_ctx", nullcontext)
    monkeypatch.setattr(skill_seeder.skill_dao, "get_by_folder_name", no_existing_skill)
    monkeypatch.setattr(skill_seeder.skill_dao, "upsert_skill_package", record_upsert)

    await skill_seeder.seed_skills()

    for folder in skill_seeder.TEMPLATE_SKILL_FOLDERS:
        files = upserts[folder]["files"]
        assert upserts[folder]["is_default"] is True
        assert [path for path, _ in files] == ["SKILL.md"]
        assert parse_skill_md_frontmatter(files[0][1]).get("name") == folder


IDLE_TRANSACTION_LIMIT_MS = 1000


@dataclass
class IdleLimitProbeStorage(MemoryStorage):
    """Storage whose first read outlasts the server's idle-in-transaction limit, without sleeping.

    It opens a probe session under the same limit, leaves it idle in a transaction, and returns once the server has
    ended it. Anything the caller left idle in a transaction before this call has by then outlived the limit too.
    """

    conninfo: str = ""
    outlasted: bool = False

    @override
    async def read_snapshot_bounded(self, key: str, max_bytes: int) -> StorageSnapshot:
        if not self.outlasted:
            self.outlasted = True
            probe = await AsyncConnection.connect(
                self.conninfo, options=f"-cidle_in_transaction_session_timeout={IDLE_TRANSACTION_LIMIT_MS}"
            )
            try:
                _ = await probe.execute("SELECT 1")
                with fail_after(10):
                    await wait_readable(probe.fileno())
            finally:
                await probe.close()
        return await super().read_snapshot_bounded(key, max_bytes)


@dataclass(frozen=True, slots=True)
class LiveDefaults:
    db: DbConnection
    conninfo: str
    agent_id: UUID


@pytest.fixture
async def live_defaults(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[LiveDefaults]:
    """A real schema with one default skill and one agent, behind a pool whose sessions end idle transactions at 1 s."""
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for the idle-in-transaction check")
    schema = f"skill_sync_qa_{uuid4().hex}"
    monkeypatch.setenv("DATABASE_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS", str(IDLE_TRANSACTION_LIMIT_MS))
    get_settings.cache_clear()
    user_id, agent_id, skill_id = uuid4(), uuid4(), uuid4()
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            await db.execute(bootstrap_db.SCHEMA_BASELINE.read_text())
            await db.execute(
                "INSERT INTO users (id, display_name, quota_message_limit) VALUES (%(id)s, 'Skill QA', 1)",
                {"id": user_id},
            )
            await db.execute(
                "INSERT INTO agents (id, name, creator_id, role_description, max_triggers, min_poll_interval_min, "
                "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
                "VALUES (%(id)s, 'Skill QA', %(user)s, '', 20, 5, 5, 'private', 'use', 240)",
                {"id": agent_id, "user": user_id},
            )
            await db.execute(
                "INSERT INTO skills (id, name, description, category, icon, folder_name, is_builtin, is_default) "
                "VALUES (%(id)s, 'Current default', '', 'general', 'x', 'current-default', true, true)",
                {"id": skill_id},
            )
            for path, content in CURRENT.items():
                await db.execute(
                    "INSERT INTO skill_files (id, skill_id, path, content) VALUES (gen_random_uuid(), %(skill)s, %(path)s, %(content)s)",
                    {"skill": skill_id, "path": path.removeprefix("current-default/"), "content": content},
                )
            isolated = make_conninfo(conninfo, options=f"-csearch_path={schema}")
            _ = await init_pool(conninfo=isolated, min_size=1, max_size=2)
            yield LiveDefaults(db, isolated, agent_id)
        finally:
            await close_pool()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')
    get_settings.cache_clear()


async def test_a_sweep_slower_than_the_idle_transaction_limit_still_records_the_sync_hash(
    live_defaults: LiveDefaults, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a real database that ends a session idle in a transaction after 1 s, and storage that outlasts that limit.
    slow = IdleLimitProbeStorage(conninfo=live_defaults.conninfo)
    monkeypatch.setattr(storage, "get_storage_backend", lambda: slow)
    # When the startup sweep pushes the default skill to the agent.
    await skill_seeder.push_default_skills_to_existing_agents()
    # Then the files were written, and the record was saved: no transaction sat idle through the storage I/O.
    assert slow.outlasted
    assert sorted(slow.written) == sorted(skill_key(live_defaults.agent_id, path) for path in CURRENT)
    recorded_value = await live_defaults.db.fetchval(
        "SELECT value FROM system_settings WHERE key = %(key)s", {"key": SETTING_KEY}
    )
    assert isinstance(recorded_value, dict)
    assert recorded_value["files"] == {path: sha256_hex(content) for path, content in CURRENT.items()}
