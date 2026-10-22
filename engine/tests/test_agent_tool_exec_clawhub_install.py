import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.api import skills
from app.core.json_types import JsonObject
from app.services.agent_tool_exec import clawhub
from app.services.storage_runtime import facade
from app.services.storage_runtime.local import LocalStorageBackend

ENTRY = "---\nname: reporting\ndescription: Reports\n---\n# Reporting"


@pytest.fixture(autouse=True)
def _isolate_install_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(facade, "_storage_backend", LocalStorageBackend(str(tmp_path / "durable")))
    monkeypatch.setattr("app.services.agent_context_cache._ttl", lambda: 0)
    monkeypatch.setattr("app.services.agent_tools._get_agent_tenant_id", AsyncMock(return_value="tenant-clawhub"))


async def test_install_github_writes_files_with_tenant_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[object, ...]] = []

    def parse(source: str) -> JsonObject:
        calls.append(("parse", source))
        return {"owner": "acme", "repo": "skills", "branch": "main", "path": "tools/reporting"}

    async def get_token(tenant_id: str) -> str:
        calls.append(("token", tenant_id))
        return "github-token"

    async def fetch(owner: str, repo: str, path: str, branch: str, token: str) -> list[JsonObject]:
        calls.append(("fetch", owner, repo, path, branch, token))
        return [{"path": "SKILL.md", "content": ENTRY}]

    monkeypatch.setattr(skills, "_parse_github_url", parse)
    monkeypatch.setattr(skills, "_get_github_token", get_token)
    monkeypatch.setattr(skills, "_fetch_github_directory", fetch)

    result = await clawhub._install_skill(
        uuid.uuid4(), tmp_path, {"source": "https://github.com/acme/skills/tree/main/tools/reporting"}
    )

    assert calls == [
        ("parse", "https://github.com/acme/skills/tree/main/tools/reporting"),
        ("token", "tenant-clawhub"),
        ("fetch", "acme", "skills", "tools/reporting", "main", "github-token"),
    ]
    assert (tmp_path / "skills" / "reporting" / "SKILL.md").read_text() == ENTRY
    assert result == (
        "✅ Skill 'reporting' installed successfully (1 files written to skills/reporting/).\n\nFiles: SKILL.md"
    )


async def test_install_clawhub_fetches_metadata_before_archive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[object, ...]] = []

    async def get_key(tenant_id: str) -> str:
        calls.append(("key", tenant_id))
        return "clawhub-key"

    async def metadata(slug: str, *, api_key: str) -> tuple[JsonObject, str]:
        calls.append(("metadata", slug, api_key))
        return {"slug": slug}, "https://clawhub.test"

    async def archive(slug: str, *, api_key: str, preferred_base: str) -> tuple[list[JsonObject], str]:
        calls.append(("archive", slug, api_key, preferred_base))
        return [{"path": "SKILL.md", "content": ENTRY}], preferred_base

    monkeypatch.setattr(skills, "_get_clawhub_key", get_key)
    monkeypatch.setattr(skills, "_fetch_clawhub_skill_meta", metadata)
    monkeypatch.setattr(skills, "_fetch_clawhub_skill_archive", archive)

    result = await clawhub._install_skill(uuid.uuid4(), tmp_path, {"source": "  market-research  "})

    assert calls == [
        ("key", "tenant-clawhub"),
        ("metadata", "market-research", "clawhub-key"),
        ("archive", "market-research", "clawhub-key", "https://clawhub.test"),
    ]
    assert (tmp_path / "skills" / "market-research" / "SKILL.md").read_text() == ENTRY
    assert result == (
        "✅ Skill 'market-research' installed successfully (1 files written to skills/market-research/).\n\nFiles: SKILL.md"
    )


@pytest.mark.parametrize("path", ["../../../escape.txt", "../../../ws-sibling/escape.txt"])
async def test_install_rejects_paths_outside_skill_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str
) -> None:
    monkeypatch.setattr(
        skills, "_parse_github_url", lambda _source: {"owner": "acme", "repo": "skill", "branch": "main", "path": ""}
    )
    monkeypatch.setattr(skills, "_get_github_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(
        skills, "_fetch_github_directory", AsyncMock(return_value=[{"path": path, "content": "must not write"}])
    )

    result = await clawhub._install_skill(uuid.uuid4(), tmp_path / "ws", {"source": "https://github.com/acme/skill"})

    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "ws-sibling" / "escape.txt").exists()
    assert result.startswith("❌")
