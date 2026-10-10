import hashlib
from contextlib import nullcontext
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from app.core.json_types import JsonObject
from app.records.skill import SkillFileRecord, SkillRecord
from app.services import skill_seeder, storage
from app.services.agent_manager import agent_manager


@dataclass
class MemoryStorage:
    files: dict[str, str] = field(default_factory=dict)
    inspected: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)

    async def is_file(self, key: str) -> bool:
        self.inspected.append(key)
        return key in self.files

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        del self.files[key]

    async def write_text(self, key: str, content: str, encoding: str = "utf-8") -> None:
        assert encoding == "utf-8"
        self.written.append(key)
        self.files[key] = content


@dataclass
class SyncSetting:
    value: JsonObject


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
        assert key == "default_skills_sync_hash"
        self.setting_reads += 1
        return self.setting

    async def list_agents(self) -> list[UUID]:
        self.agent_reads += 1
        return self.agent_ids

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
            SkillFileRecord(id=uuid4(), skill_id=skill_id, path="SKILL.md", content="current instructions"),
            SkillFileRecord(id=uuid4(), skill_id=skill_id, path="scripts/run.py", content="print('current')"),
        ],
    )
    state = Reconciliation(defaults=[skill], agent_ids=[uuid4(), uuid4()])
    monkeypatch.setattr(skill_seeder, "connection_ctx", nullcontext)
    monkeypatch.setattr(skill_seeder.skill_dao, "list_defaults_with_files", state.list_defaults)
    monkeypatch.setattr(skill_seeder.system_setting_dao, "get_by_key", state.get_setting)
    monkeypatch.setattr(skill_seeder.system_setting_dao, "create", state.create_setting)
    monkeypatch.setattr(skill_seeder.system_setting_dao, "update", state.update_setting)
    monkeypatch.setattr(skill_seeder.agent_dao, "list_all_ids", state.list_agents)
    monkeypatch.setattr(storage, "get_storage_backend", state.get_storage)
    return state


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
    expected_hash = hashlib.sha256(b"current-default").hexdigest()

    # When: the actual startup reconciliation runs.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then: obsolete paths never cross the storage boundary, current files and hash are persisted.
    assert set(obsolete_keys).isdisjoint(reconciliation.storage.inspected)
    assert reconciliation.storage.deleted == []
    assert reconciliation.storage.files == {
        **dict.fromkeys(obsolete_keys, "user-owned obsolete content"),
        **expected_files,
    }
    assert set(reconciliation.storage.written) == set(expected_files)
    expected_value: JsonObject = {"value": {"hash": expected_hash}}
    if not existing_setting:
        expected_value["key"] = "default_skills_sync_hash"
    assert reconciliation.persisted == [expected_value]


async def test_reconciliation_skips_agents_and_storage_when_hash_is_unchanged(reconciliation: Reconciliation) -> None:
    # Given: the stored hash matches the current default folders.
    reconciliation.setting = SyncSetting(value={"hash": hashlib.sha256(b"current-default").hexdigest()})

    # When.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then.
    assert reconciliation.agent_reads == reconciliation.storage_reads == 0
    assert reconciliation.persisted == []


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


async def test_reconciliation_preserves_existing_skill_when_first_file_exists(reconciliation: Reconciliation) -> None:
    # Given: existing first files keep their content; missing secondary files are not backfilled.
    existing_files = {
        f"{agent_manager._agent_storage_prefix(agent_id)}/skills/current-default/SKILL.md": "customized instructions"
        for agent_id in reconciliation.agent_ids
    }
    reconciliation.storage.files.update(existing_files)
    reconciliation.defaults.append(SkillRecord(id=uuid4(), name="Empty", folder_name="empty", is_default=True))

    # When.
    await skill_seeder.push_default_skills_to_existing_agents()

    # Then.
    assert reconciliation.storage.files == existing_files
    assert reconciliation.storage.written == []
    assert reconciliation.persisted == [
        {"key": "default_skills_sync_hash", "value": {"hash": hashlib.sha256(b"current-defaultempty").hexdigest()}}
    ]
