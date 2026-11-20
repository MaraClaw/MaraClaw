import hashlib
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, override
from uuid import UUID, uuid4

import pytest

from app.core.json_types import JsonObject
from app.records.skill import SkillFileRecord, SkillRecord
from app.services import skill_seeder, storage
from app.services.agent_manager import agent_manager
from app.services.skill_provider import parse_skill_md_frontmatter
from app.services.storage_runtime.quota_errors import StorageQuotaExceededError


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
class QuotaFullStorage(MemoryStorage):
    blocked_prefix: str | None = None

    @override
    async def write_text(self, key: str, content: str, encoding: str = "utf-8") -> None:
        if self.blocked_prefix is not None and key.startswith(self.blocked_prefix):
            raise StorageQuotaExceededError(limit_bytes=1, charged_bytes=1, size_bytes=len(content))
        await super().write_text(key, content, encoding)


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


@pytest.mark.parametrize("stored_hash", ["matching", "stale"])
async def test_content_only_refresh_preserves_existing_default_workspace(
    reconciliation: Reconciliation, stored_hash: str
) -> None:
    reconciliation.setting = SyncSetting(
        value={"hash": hashlib.sha256(b"current-default").hexdigest() if stored_hash == "matching" else "old"}
    )
    existing = {
        f"{agent_manager._agent_storage_prefix(agent_id)}/skills/current-default/{file.path}": "workspace customization"
        for agent_id in reconciliation.agent_ids
        for file in reconciliation.defaults[0].files
    }
    reconciliation.storage.files.update(existing)
    reconciliation.defaults[0].files[0].content = "refreshed registry instructions"
    await skill_seeder.push_default_skills_to_existing_agents()
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

    # Then: the retried agent gets the skill and the hash is recorded.
    assert storage.files == {**skill_files(blocked), **skill_files(healthy)}
    assert reconciliation.persisted == [
        {"key": "default_skills_sync_hash", "value": {"hash": hashlib.sha256(b"current-default").hexdigest()}}
    ]


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
