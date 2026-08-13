import builtins
import importlib
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.json_types import JsonObject
from app.services.agent_tools import ToolParameters


def _target():
    return importlib.import_module("app.services.agent_tool_exec.clawhub")


def _skills():
    return importlib.import_module("app.api.skills")


def _patch_tenant(monkeypatch: pytest.MonkeyPatch, tenant_id: str = "tenant-clawhub") -> None:
    async def get_tenant_id(_agent_id: uuid.UUID) -> str:
        return tenant_id

    monkeypatch.setattr("app.services.agent_tools._get_agent_tenant_id", get_tenant_id)


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("search", {"query": "  "}, "Missing required argument 'query'"),
        (
            "install",
            {"source": "  "},
            "❌ Missing required argument 'source'. Provide a ClawHub slug (e.g. 'market-research') or a GitHub URL.",
        ),
    ],
)
async def test_required_input_returns_before_api_import(
    monkeypatch: pytest.MonkeyPatch, name: str, arguments: ToolParameters, expected: str, tmp_path: Path
) -> None:
    target = _target()
    original_import = builtins.__import__

    def guarded_import(module_name: str, *args, **kwargs):
        if module_name == "app.api.skills":
            raise AssertionError("API helpers must not import before required input validation")
        return original_import(module_name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    result = (
        await target._search_clawhub(uuid.uuid4(), arguments)
        if name == "search"
        else await target._install_skill(uuid.uuid4(), tmp_path, arguments)
    )

    assert result == expected


async def test_search_formats_results_and_preserves_request_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _target()
    skills = _skills()
    _patch_tenant(monkeypatch)
    endpoint = object()
    calls: list[tuple[object, str, JsonObject]] = []

    async def get_key(tenant_id: str) -> str:
        assert tenant_id == "tenant-clawhub"
        return "clawhub-key"

    async def fetch(search_endpoint: object, *, api_key: str, params: JsonObject) -> tuple[JsonObject, str]:
        calls.append((search_endpoint, api_key, params))
        return {
            "results": [
                {"displayName": "Research", "slug": "research", "summary": "a" * 121, "updatedAt": 1704110400000},
                {"slug": "brief"},
            ]
        }, "https://clawhub.test"

    monkeypatch.setattr(skills, "_clawhub_search_endpoint", endpoint)
    monkeypatch.setattr(skills, "_get_clawhub_key", get_key)
    monkeypatch.setattr(skills, "_fetch_clawhub_json", fetch)

    result = await target._search_clawhub(uuid.uuid4(), {"query": "  market research  "})

    assert calls == [(endpoint, "clawhub-key", {"q": "market research"})]
    assert result == (
        "Found 2 skill(s) matching 'market research':\n\n"
        "• **Research** (`research`) | Updated: 2024-01-01\n"
        f"  {'a' * 120}\n"
        "• **brief** (`brief`)\n\n"
        'To install a skill, use: install_skill(source="<slug>")'
    )


async def test_search_ignores_malformed_summary_and_updated_at(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _target()
    skills = _skills()
    _patch_tenant(monkeypatch)

    async def get_key(_tenant_id: str) -> str:
        return "key"

    async def fetch(*_args, **_kwargs) -> tuple[JsonObject, str]:
        return {
            "results": [
                {"displayName": "Malformed", "slug": "malformed", "summary": 20, "updatedAt": "not-a-timestamp"}
            ]
        }, "https://clawhub.test"

    monkeypatch.setattr(skills, "_get_clawhub_key", get_key)
    monkeypatch.setattr(skills, "_fetch_clawhub_json", fetch)

    result = await target._search_clawhub(uuid.uuid4(), {"query": "topic"})

    assert "Malformed" in result
    assert "`malformed`" in result
    assert "20" not in result
    assert "not-a-timestamp" not in result


async def test_search_truncates_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _target()
    skills = _skills()
    _patch_tenant(monkeypatch)

    async def get_key(_tenant_id: str) -> str:
        return "key"

    async def fetch(*_args: object, **_kwargs: object) -> tuple[JsonObject, str]:
        raise RuntimeError("x" * 240)

    monkeypatch.setattr(skills, "_get_clawhub_key", get_key)
    monkeypatch.setattr(skills, "_fetch_clawhub_json", fetch)

    assert await target._search_clawhub(uuid.uuid4(), {"query": "topic"}) == f"❌ ClawHub search error: {'x' * 200}"


async def test_search_reports_no_matching_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _target()
    skills = _skills()
    _patch_tenant(monkeypatch)

    async def get_key(_tenant_id: str) -> str:
        return "key"

    async def fetch(*_args: object, **_kwargs: object) -> tuple[JsonObject, str]:
        return {"results": []}, "https://clawhub.test"

    monkeypatch.setattr(skills, "_get_clawhub_key", get_key)
    monkeypatch.setattr(skills, "_fetch_clawhub_json", fetch)

    assert await target._search_clawhub(uuid.uuid4(), {"query": "missing"}) == "No skills found matching 'missing'."


async def test_install_github_writes_files_with_tenant_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = _target()
    skills = _skills()
    _patch_tenant(monkeypatch)
    calls: list[tuple[object, ...]] = []

    def parse(source: str) -> JsonObject:
        calls.append(("parse", source))
        return {"owner": "acme", "repo": "skills", "branch": "main", "path": "tools/reporting"}

    async def get_token(tenant_id: str) -> str:
        calls.append(("token", tenant_id))
        return "github-token"

    async def fetch(owner: str, repo: str, path: str, branch: str, token: str) -> list[JsonObject]:
        calls.append(("fetch", owner, repo, path, branch, token))
        return [{"path": "SKILL.md", "content": "# Reporting"}]

    monkeypatch.setattr(skills, "_parse_github_url", parse)
    monkeypatch.setattr(skills, "_get_github_token", get_token)
    monkeypatch.setattr(skills, "_fetch_github_directory", fetch)

    result = await target._install_skill(
        uuid.uuid4(),
        tmp_path,
        {"source": "https://github.com/acme/skills/tree/main/tools/reporting"},
    )

    assert calls == [
        ("parse", "https://github.com/acme/skills/tree/main/tools/reporting"),
        ("token", "tenant-clawhub"),
        ("fetch", "acme", "skills", "tools/reporting", "main", "github-token"),
    ]
    assert (tmp_path / "skills" / "reporting" / "SKILL.md").read_text() == "# Reporting"
    assert result == (
        "✅ Skill 'reporting' installed successfully (1 files written to skills/reporting/).\n\nFiles: SKILL.md"
    )


async def test_install_clawhub_fetches_metadata_before_archive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = _target()
    skills = _skills()
    _patch_tenant(monkeypatch)
    calls: list[tuple[object, ...]] = []

    async def get_key(tenant_id: str) -> str:
        calls.append(("key", tenant_id))
        return "clawhub-key"

    async def metadata(slug: str, *, api_key: str) -> tuple[JsonObject, str]:
        calls.append(("metadata", slug, api_key))
        return {"slug": slug}, "https://clawhub.test"

    async def archive(slug: str, *, api_key: str, preferred_base: str) -> tuple[list[JsonObject], str]:
        calls.append(("archive", slug, api_key, preferred_base))
        return [{"path": "SKILL.md", "content": "# Market Research"}], preferred_base

    monkeypatch.setattr(skills, "_get_clawhub_key", get_key)
    monkeypatch.setattr(skills, "_fetch_clawhub_skill_meta", metadata)
    monkeypatch.setattr(skills, "_fetch_clawhub_skill_archive", archive)

    result = await target._install_skill(uuid.uuid4(), tmp_path, {"source": "  market-research  "})

    assert calls == [
        ("key", "tenant-clawhub"),
        ("metadata", "market-research", "clawhub-key"),
        ("archive", "market-research", "clawhub-key", "https://clawhub.test"),
    ]
    assert (tmp_path / "skills" / "market-research" / "SKILL.md").read_text() == "# Market Research"
    assert result == (
        "✅ Skill 'market-research' installed successfully (1 files written to skills/market-research/).\n\nFiles: SKILL.md"
    )


@pytest.mark.parametrize("source", ["../../ws-escape", ".", "..", "nested/skill", r"nested\skill"])
async def test_install_rejects_slug_that_is_not_a_safe_folder_component(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str
) -> None:
    target = _target()
    skills = _skills()
    _patch_tenant(monkeypatch)

    async def get_key(_tenant_id: str) -> str:
        return "key"

    async def metadata(*_args: object, **_kwargs: object) -> tuple[JsonObject, str]:
        return {}, "https://clawhub.test"

    async def archive(*_args: object, **_kwargs: object) -> tuple[list[JsonObject], str]:
        return [{"path": "SKILL.md", "content": "must not write"}], "https://clawhub.test"

    monkeypatch.setattr(skills, "_get_clawhub_key", get_key)
    monkeypatch.setattr(skills, "_fetch_clawhub_skill_meta", metadata)
    monkeypatch.setattr(skills, "_fetch_clawhub_skill_archive", archive)

    result = await target._install_skill(uuid.uuid4(), tmp_path, {"source": source})

    assert result == "❌ Invalid skill folder name."
    assert not (tmp_path.parent / "ws-escape").exists()
    assert not (tmp_path / "skills").exists()


async def test_install_skips_paths_resolving_outside_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = _target()
    skills = _skills()
    _patch_tenant(monkeypatch)

    def parse(_source: str) -> JsonObject:
        return {"owner": "acme", "repo": "skills", "branch": "main", "path": ""}

    async def get_token(_tenant_id: str) -> str:
        return "token"

    async def fetch(*_args: object) -> list[JsonObject]:
        return [{"path": "../../../escape.txt", "content": "must not write"}]

    monkeypatch.setattr(skills, "_parse_github_url", parse)
    monkeypatch.setattr(skills, "_get_github_token", get_token)
    monkeypatch.setattr(skills, "_fetch_github_directory", fetch)

    result = await target._install_skill(uuid.uuid4(), tmp_path, {"source": "https://github.com/acme/skills"})

    assert not (tmp_path.parent / "escape.txt").exists()
    assert result == "✅ Skill 'skills' installed successfully (0 files written to skills/skills/).\n\nFiles: "


async def test_install_skips_sibling_prefix_archive_escape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = _target()
    skills = _skills()
    _patch_tenant(monkeypatch)
    workspace = tmp_path / "ws"
    sibling = tmp_path / "ws-sibling"

    def parse(_source: str) -> JsonObject:
        return {"owner": "acme", "repo": "skill", "branch": "main", "path": ""}

    async def get_token(_tenant_id: str) -> str:
        return "token"

    async def fetch(*_args: object) -> list[JsonObject]:
        return [{"path": "../../../ws-sibling/escape.txt", "content": "must not write"}]

    monkeypatch.setattr(skills, "_parse_github_url", parse)
    monkeypatch.setattr(skills, "_get_github_token", get_token)
    monkeypatch.setattr(skills, "_fetch_github_directory", fetch)

    result = await target._install_skill(uuid.uuid4(), workspace, {"source": "https://github.com/acme/skill"})

    assert not (sibling / "escape.txt").exists()
    assert result == "✅ Skill 'skill' installed successfully (0 files written to skills/skill/).\n\nFiles: "


@pytest.mark.parametrize(
    ("metadata_error", "archive_files", "expected"),
    [
        ("x" * 240, None, f"Failed to connect to ClawHub: {'x' * 200}"),
        (None, [], "❌ No files found for skill 'empty-skill' in the ClawHub archive."),
    ],
)
async def test_install_reports_provider_and_no_file_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    metadata_error: str | None,
    archive_files: list[JsonObject] | None,
    expected: str,
) -> None:
    target = _target()
    skills = _skills()
    _patch_tenant(monkeypatch)

    async def get_key(_tenant_id: str) -> str:
        return "key"

    async def metadata(*_args: object, **_kwargs: object) -> tuple[JsonObject, str]:
        if metadata_error is not None:
            raise RuntimeError(metadata_error)
        return {}, "https://clawhub.test"

    async def archive(*_args: object, **_kwargs: object) -> tuple[list[JsonObject], str]:
        assert archive_files is not None
        return archive_files, "https://clawhub.test"

    monkeypatch.setattr(skills, "_get_clawhub_key", get_key)
    monkeypatch.setattr(skills, "_fetch_clawhub_skill_meta", metadata)
    monkeypatch.setattr(skills, "_fetch_clawhub_skill_archive", archive)

    assert await target._install_skill(uuid.uuid4(), tmp_path, {"source": "empty-skill"}) == expected


async def test_facades_preserve_workspace_and_arguments_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent_tools = importlib.import_module("app.services.agent_tools")
    clawhub = _target()
    agent_id = uuid.uuid4()
    search_arguments: ToolParameters = {"query": "same object"}
    install_arguments: ToolParameters = {"source": "same object"}
    received: list[tuple[object, ...]] = []

    async def search(observed_agent_id: uuid.UUID, arguments: ToolParameters) -> str:
        received.append(("search", observed_agent_id, arguments))
        return "searched"

    async def install(observed_agent_id: uuid.UUID, workspace: Path, arguments: ToolParameters) -> str:
        received.append(("install", observed_agent_id, workspace, arguments))
        return "installed"

    monkeypatch.setattr(clawhub, "_search_clawhub", search)
    monkeypatch.setattr(clawhub, "_install_skill", install)

    assert await agent_tools._search_clawhub(agent_id, search_arguments) == "searched"
    assert await agent_tools._install_skill(agent_id, tmp_path, install_arguments) == "installed"
    assert received == [("search", agent_id, search_arguments), ("install", agent_id, tmp_path, install_arguments)]
    assert received[0][2] is search_arguments
    assert received[1][3] is install_arguments


def test_owner_uses_agent_tools_tenant_lookup() -> None:
    import app.services.agent_tool_exec.clawhub as clawhub
    import app.services.agent_tools as agent_tools

    assert clawhub.agent_tools is agent_tools


async def test_dispatcher_routes_literal_clawhub_name_and_direct_rejects_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent_tools = importlib.import_module("app.services.agent_tools")
    agent_id = uuid.uuid4()
    arguments: ToolParameters = {"query": "route"}
    received: list[tuple[object, ...]] = []

    async def tenant(_agent_id: uuid.UUID) -> str:
        return "tenant"

    async def search(observed_agent_id: uuid.UUID, observed_arguments: ToolParameters) -> str:
        received.append((observed_agent_id, observed_arguments))
        return "routed"

    async def log_activity(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant)
    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _agent_id: tmp_path)
    monkeypatch.setattr(agent_tools, "_search_clawhub", search)
    monkeypatch.setattr(agent_tools, "resolve_tool_handler", lambda _tool_name: None)
    monkeypatch.setitem(sys.modules, "app.services.activity_logger", SimpleNamespace(log_activity=log_activity))

    assert await agent_tools.execute_tool("search_clawhub", arguments, agent_id, uuid.uuid4()) == "routed"
    assert received == [(agent_id, arguments)]
    assert received[0][1] is arguments
    assert await agent_tools._execute_tool_direct("search_clawhub", {}, agent_id) == (
        "Tool search_clawhub does not support post-approval execution"
    )
