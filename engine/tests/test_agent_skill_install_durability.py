from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.api import skills
from app.services import agent_context, agent_context_cache, agent_manager, agent_tools
from app.services.agent_tool_exec import clawhub
from app.services.skill_provider import SkillFilePayload
from app.services.storage_runtime import facade
from app.services.storage_runtime.local import LocalStorageBackend

SKILL = "---\nname: reporting\ndescription: Generate reports\n---\n# Reporting\n"
FILES: list[SkillFilePayload] = [
    {"path": "SKILL.md", "content": SKILL},
    {"path": "scripts/report.py", "content": "print('report')\n"},
    {"path": "references/schema.json", "content": '{"version":2}\n'},
]


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalStorageBackend:
    backend = LocalStorageBackend(str(tmp_path / "durable"))
    monkeypatch.setattr(facade, "_storage_backend", backend)
    monkeypatch.setattr(agent_manager.settings, "STORAGE_LOCAL_ROOT", str(tmp_path / "live"))
    monkeypatch.setattr(agent_context_cache, "_ttl", lambda: 0)
    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", AsyncMock(return_value="tenant"))
    monkeypatch.setattr(skills, "_get_clawhub_key", AsyncMock(return_value="key"))
    monkeypatch.setattr(skills, "_fetch_clawhub_skill_meta", AsyncMock(return_value=({}, "https://clawhub.test")))
    monkeypatch.setattr(skills, "_fetch_clawhub_skill_archive", AsyncMock(return_value=(FILES, "https://clawhub.test")))
    return backend


async def _seed(storage: LocalStorageBackend, agent_id: uuid.UUID, ws: Path) -> None:
    for path, content in {"SKILL.md": SKILL + "old instructions\n", "scripts/old.py": "old script"}.items():
        await storage.write_text(f"{agent_id}/skills/reporting/{path}", content)
        target = ws / "skills" / "reporting" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def _tree(path: Path) -> dict[str, bytes]:
    return {file.relative_to(path).as_posix(): file.read_bytes() for file in path.rglob("*") if file.is_file()}


@pytest.mark.parametrize("fresh_host", [False, True])
async def test_install_survives_storage_rehydration(
    storage: LocalStorageBackend, tmp_path: Path, fresh_host: bool
) -> None:
    # Given an old durable skill and its materialized guest copy.
    agent_id = uuid.uuid4()
    ws = tmp_path / "live" / str(agent_id)
    await _seed(storage, agent_id, ws)

    # When the tool installs a complete replacement and startup rehydrates it.
    result = await clawhub._install_skill(agent_id, ws, {"source": "reporting"})
    if fresh_host:
        shutil.rmtree(ws)
    manager = agent_manager.AgentManager.__new__(agent_manager.AgentManager)
    await manager._materialize_agent_dir(agent_id)

    # Then every payload file survives, obsolete files disappear, and discovery sees it.
    assert result.startswith("✅")
    expected = {file["path"]: file["content"].encode() for file in FILES}
    assert _tree(ws / "skills" / "reporting") == expected
    assert _tree(Path(storage.root) / str(agent_id) / "skills" / "reporting") == expected
    index = await agent_context._load_skills_index(agent_id)
    assert "skills/reporting/SKILL.md" in index


async def test_storage_failure_preserves_old_skill(
    monkeypatch: pytest.MonkeyPatch, storage: LocalStorageBackend, tmp_path: Path
) -> None:
    # Given a working skill and a provider that fails after one successful write.
    agent_id = uuid.uuid4()
    ws = tmp_path / "live" / str(agent_id)
    await _seed(storage, agent_id, ws)
    before = _tree(ws / "skills" / "reporting")
    write = storage.write_bytes
    failed = False

    async def fail_once(key: str, data: bytes, content_type: str | None = None) -> None:
        nonlocal failed
        if key.endswith("references/schema.json") and not failed:
            failed = True
            raise OSError("provider unavailable")
        await write(key, data, content_type)

    monkeypatch.setattr(storage, "write_bytes", fail_once)

    # When publication cannot finish.
    result = await clawhub._install_skill(agent_id, ws, {"source": "reporting"})

    # Then neither the active guest copy nor the durable copy changes.
    assert result.startswith("❌")
    assert _tree(ws / "skills" / "reporting") == before
    assert _tree(Path(storage.root) / str(agent_id) / "skills" / "reporting") == before


async def test_staging_failure_preserves_old_skill(
    monkeypatch: pytest.MonkeyPatch, storage: LocalStorageBackend, tmp_path: Path
) -> None:
    # Given a working skill and a local write failure on an auxiliary file.
    agent_id = uuid.uuid4()
    ws = tmp_path / "live" / str(agent_id)
    await _seed(storage, agent_id, ws)
    before = _tree(ws / "skills" / "reporting")
    original = Path.mkdir

    def fail_mkdir(path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        if path.name == "references":
            raise OSError("disk full")
        original(path, mode, parents, exist_ok)

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    # When local staging cannot finish.
    result = await clawhub._install_skill(agent_id, ws, {"source": "reporting"})

    # Then the old skill remains complete in both locations.
    assert result.startswith("❌")
    assert _tree(ws / "skills" / "reporting") == before
    assert _tree(Path(storage.root) / str(agent_id) / "skills" / "reporting") == before


@pytest.mark.parametrize(
    "payload",
    [
        [{"path": "README.md", "content": "not a skill"}],
        [{"path": "SKILL.md", "content": ""}],
        [{"path": "SKILL.md", "content": "---\nname: [broken\n---\n"}],
        [{"path": "SKILL.md", "content": "---\nname: reporting\ndescription: ''\n---\n"}],
        [*FILES, {"path": "../../../escape.txt", "content": "unsafe"}],
        [*FILES, {"path": "/absolute.txt", "content": "unsafe"}],
        [*FILES, {"path": "..\\escape.txt", "content": "unsafe"}],
        [*FILES, {"path": "SKILL.md", "content": "duplicate"}],
        [*FILES, {"path": "scripts", "content": "file conflicts with folder"}],
    ],
    ids=[
        "missing-entry",
        "empty-entry",
        "bad-yaml",
        "blank-description",
        "traversal",
        "absolute",
        "backslash",
        "duplicate",
        "collision",
    ],
)
async def test_invalid_payload_never_replaces_working_skill(
    monkeypatch: pytest.MonkeyPatch, storage: LocalStorageBackend, payload: list[SkillFilePayload]
) -> None:
    # Given an invalid provider payload and a previously installed skill.
    agent_id = uuid.uuid4()
    ws = storage.root.parent / "live" / str(agent_id)
    await _seed(storage, agent_id, ws)
    before = _tree(ws / "skills" / "reporting")
    monkeypatch.setattr(
        skills, "_fetch_clawhub_skill_archive", AsyncMock(return_value=(payload, "https://clawhub.test"))
    )

    # When the invalid replacement is installed.
    result = await clawhub._install_skill(agent_id, ws, {"source": "reporting"})

    # Then failure is reported without publishing any portion of the payload.
    assert result.startswith("❌")
    assert _tree(ws / "skills" / "reporting") == before
    assert _tree(Path(storage.root) / str(agent_id) / "skills" / "reporting") == before


@pytest.mark.parametrize("fails", [False, True])
async def test_install_supports_storage_sharing_guest_tree(
    monkeypatch: pytest.MonkeyPatch, storage: LocalStorageBackend, fails: bool
) -> None:
    # Given local storage that is also the active guest state directory.
    agent_id = uuid.uuid4()
    ws = storage.root / str(agent_id)
    await _seed(storage, agent_id, ws)
    before = _tree(ws / "skills" / "reporting")
    write = storage.write_bytes
    failed = False

    async def fail_once(key: str, data: bytes, content_type: str | None = None) -> None:
        nonlocal failed
        if fails and key.endswith("references/schema.json") and not failed:
            failed = True
            raise OSError("disk unavailable")
        await write(key, data, content_type)

    monkeypatch.setattr(storage, "write_bytes", fail_once)

    # When the skill is installed through the same physical storage tree.
    result = await clawhub._install_skill(agent_id, ws, {"source": "reporting"})

    # Then success publishes the full tree, and failure preserves the prior tree.
    assert result.startswith("❌" if fails else "✅")
    expected = before if fails else {file["path"]: file["content"].encode() for file in FILES}
    assert _tree(ws / "skills" / "reporting") == expected


class _RedisValues:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        self.values[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        for key in keys:
            self.values.pop(key, None)
        return len(keys)

    async def incr(self, key: str) -> int:
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value


async def test_success_invalidates_cached_skills_index(
    monkeypatch: pytest.MonkeyPatch, storage: LocalStorageBackend, tmp_path: Path
) -> None:
    # Given a cached skills index at the current version.
    agent_id = uuid.uuid4()
    monkeypatch.setattr(agent_context_cache, "_ttl", lambda: 60)
    monkeypatch.setattr(agent_context_cache, "get_redis", AsyncMock(return_value=_RedisValues()))
    await agent_context_cache.set_cached_text(agent_id, "skills", "cached-index", observed_ver="0")

    # When a complete skill is installed.
    result = await clawhub._install_skill(agent_id, tmp_path / "live" / str(agent_id), {"source": "reporting"})

    # Then stale fills cannot replace the invalidated index.
    assert result.startswith("✅")
    await agent_context_cache.set_cached_text(agent_id, "skills", "stale-index", observed_ver="0")
    assert await agent_context_cache.read_cached_text(agent_id, "skills") == (None, "1")
