"""Gating tests for Linkup skill seed and removal of built-in search-engine tools."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.config import Settings
from app.core.json_types import json_as_str_or, json_loads_object, json_object_from, str_list_from_row
from app.records.agent import AgentRecord
from app.services import agent_manager as agent_manager_module, linkup_runtime
from app.services.agent_manager import AgentManager
from app.services.agent_tool_exec import registry
from app.services.agent_tools_definitions import AGENT_TOOLS
from app.services.linkup.tokens import parse_proxy_token
from app.services.linkup_runtime import (
    LINKUP_EXTRACT_SKILL_FOLDER,
    LINKUP_FETCH_SKILL_FOLDER,
    LINKUP_RESEARCH_SKILL_FOLDER,
    LINKUP_SEARCH_SKILL_FOLDER,
    linkup_default_skill_folder_names,
    linkup_skill_folder_names,
    linkup_skill_root,
    load_linkup_manifest,
    seed_linkup_skills,
)
from app.services.tool_definitions import BUILTIN_TOOLS
from app.services.tool_seeder import SYNC_IS_DEFAULT_TOOL_NAMES

DEFAULT_LINKUP_SKILLS = (
    LINKUP_SEARCH_SKILL_FOLDER,
    LINKUP_FETCH_SKILL_FOLDER,
    LINKUP_RESEARCH_SKILL_FOLDER,
    LINKUP_EXTRACT_SKILL_FOLDER,
)

REMOVED_SEARCH_TOOL_NAMES = (
    "web_search",
    "bing_search",
    "duckduckgo_search",
    "tavily_search",
    "google_search",
    "jina_search",
    "exa_search",
    "jina_read",
)


def test_removed_search_tools_are_absent_from_shipped_catalog_and_registry() -> None:
    # Given: the shipped builtin catalog, LLM catalog, registry, and seeder lists.
    importlib_agent_tools()
    builtin_names = {tool["name"] for tool in BUILTIN_TOOLS}
    tools: list[Any] = AGENT_TOOLS
    catalog_names = {tool["function"]["name"] for tool in tools}

    # When / Then: the old search-engine family is gone from every shipped surface.
    assert set(REMOVED_SEARCH_TOOL_NAMES).isdisjoint(builtin_names)
    assert set(REMOVED_SEARCH_TOOL_NAMES).isdisjoint(catalog_names)
    assert set(REMOVED_SEARCH_TOOL_NAMES).isdisjoint(registry.TOOL_HANDLERS)
    assert set(REMOVED_SEARCH_TOOL_NAMES).isdisjoint(SYNC_IS_DEFAULT_TOOL_NAMES)
    seeder_source = Path("app/services/tool_seeder.py").read_text(encoding="utf-8")
    for name in REMOVED_SEARCH_TOOL_NAMES:
        assert f'"{name}"' not in seeder_source
    assert Path("app/services/tool_definitions/removed.py").exists() is False
    assert Path("app/services/agent_tool_exec/web_search.py").exists() is False
    assert Path("app/services/agent_tool_exec/search_providers.py").exists() is False
    assert "jina_read" not in registry.TOOL_HANDLERS


def importlib_agent_tools() -> None:
    import importlib

    importlib.import_module("app.services.agent_tools")


def _shell_requests(content: str) -> list[list[str]]:
    return [
        shlex.split(command.replace("\\\n", ""))
        for block in re.findall(r"```shell\n(.*?)```", content, re.DOTALL)
        for command in block.strip().split("\n\n")
    ]


@pytest.mark.parametrize("folder_name", DEFAULT_LINKUP_SKILLS)
def test_official_linkup_skill_payload_calls_authenticated_proxy(folder_name: str) -> None:
    manifest = load_linkup_manifest()
    skill_text = (linkup_skill_root() / folder_name / "SKILL.md").read_text(encoding="utf-8")

    requests = _shell_requests(skill_text)

    assert folder_name in str_list_from_row(manifest.get("skills"))
    assert folder_name in str_list_from_row(manifest.get("default_skills"))
    assert folder_name in linkup_skill_folder_names()
    assert folder_name in linkup_default_skill_folder_names()
    assert linkup_runtime._frontmatter_value(skill_text, "name", "") == folder_name
    endpoint = folder_name.removeprefix("linkup-")
    assert requests
    for request in requests:
        assert request[0] == "curl"
        urls = [arg for arg in request if arg.startswith(("$LINKUP_API_BASE", "https://", "http://"))]
        assert len(urls) == 1
        assert urls[0] == f"$LINKUP_API_BASE/v1/{endpoint}" or urls[0].startswith(f"$LINKUP_API_BASE/v1/{endpoint}/")
        headers = [request[index + 1] for index, arg in enumerate(request) if arg == "-H"]
        assert "Authorization: Bearer $LINKUP_PROXY_TOKEN" in headers
    submission = requests[0]
    assert submission[submission.index("-X") + 1] == "POST"
    body = json_loads_object(submission[submission.index("-d") + 1])
    assert ("q" if endpoint in {"search", "research"} else "url") in body


@pytest.mark.asyncio
async def test_seed_linkup_skills_upserts_official_default_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the real seeder path with a captured skill_dao.
    upserts: list[dict[str, Any]] = []

    async def fake_get_by_folder_name(_folder_name: str):
        return None

    async def fake_upsert_skill_package(**kwargs):
        upserts.append(kwargs)
        return SimpleNamespace(id=uuid4(), folder_name=kwargs["folder_name"])

    monkeypatch.setattr(linkup_runtime.skill_dao, "get_by_folder_name", fake_get_by_folder_name)
    monkeypatch.setattr(linkup_runtime.skill_dao, "upsert_skill_package", fake_upsert_skill_package)

    # When
    processed = await seed_linkup_skills()

    # Then
    assert processed == len(DEFAULT_LINKUP_SKILLS)
    assert {item["folder_name"] for item in upserts} == set(DEFAULT_LINKUP_SKILLS)
    by_folder = {item["folder_name"]: item for item in upserts}
    for folder_name in DEFAULT_LINKUP_SKILLS:
        package = by_folder[folder_name]
        assert package["name"] == folder_name
        assert package["is_builtin"] is True
        assert package["is_default"] is True
        files = dict(package["files"])
        assert files["SKILL.md"] == (linkup_skill_root() / folder_name / "SKILL.md").read_text(encoding="utf-8")
        assert _shell_requests(files["SKILL.md"])
        assert any(path.startswith("references/") for path in files)


@pytest.mark.parametrize("upstream_key", ["", "lk_raw_must_not_leak"])
@pytest.mark.parametrize(
    ("proxy_base", "expected_base"),
    [
        ("", "https://deployed-engine.example/guest/api/linkup"),
        (" \t", "https://deployed-engine.example/guest/api/linkup"),
        (" https://dedicated-proxy.example/linkup/// ", "https://dedicated-proxy.example/linkup"),
    ],
)
def test_generate_openclaw_config_enables_authenticated_linkup_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, upstream_key: str, proxy_base: str, expected_base: str
) -> None:
    monkeypatch.setattr(agent_manager_module.settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_API_KEY", upstream_key)
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_PROXY_ENABLED", True)
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_PROXY_BASE_URL", proxy_base)
    monkeypatch.setattr(
        agent_manager_module.settings, "OPENCLAW_ENGINE_BASE_URL", "https://deployed-engine.example/guest/"
    )
    monkeypatch.setattr(agent_manager_module.settings, "SECRET_KEY", "cfg-secret")
    manager = AgentManager.__new__(AgentManager)
    agent = AgentRecord(id=uuid4(), name="Search Agent", creator_id=uuid4())

    config = manager._generate_openclaw_config(agent, model=None)

    assert "lk_raw_must_not_leak" not in json.dumps(config)
    skills = json_object_from(json_object_from(config["skills"])["entries"])
    assert set(skills) == set(DEFAULT_LINKUP_SKILLS)
    for folder_name in DEFAULT_LINKUP_SKILLS:
        entry = json_object_from(skills[folder_name])
        assert entry["enabled"] is True
        env = json_object_from(entry["env"])
        assert set(env) == {"LINKUP_API_BASE", "LINKUP_PROXY_TOKEN"}
        assert env["LINKUP_API_BASE"] == expected_base
        assert parse_proxy_token(json_as_str_or(env["LINKUP_PROXY_TOKEN"]), secret="cfg-secret") == agent.id
    tools = json_object_from(config["tools"])
    assert "web_search" in str_list_from_row(tools["deny"])
    assert json_object_from(json_object_from(tools["web"])["search"])["enabled"] is False


def test_generate_openclaw_config_disables_linkup_without_raw_key_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(agent_manager_module.settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_API_KEY", "lk_raw_must_not_leak")
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_PROXY_ENABLED", False)
    monkeypatch.setattr(
        agent_manager_module.settings,
        "LINKUP_PROXY_BASE_URL",
        "http://maraclaw-engine:8000/api/linkup",
    )
    manager = AgentManager.__new__(AgentManager)
    agent = AgentRecord(id=uuid4(), name="Search Agent", creator_id=uuid4())

    config = manager._generate_openclaw_config(agent, model=None)

    assert "lk_raw_must_not_leak" not in json.dumps(config)
    skills = json_object_from(json_object_from(config["skills"])["entries"])
    assert set(skills) == set(DEFAULT_LINKUP_SKILLS)
    for folder_name in DEFAULT_LINKUP_SKILLS:
        assert skills[folder_name] == {"enabled": False, "env": {}}
    assert "web_search" in str_list_from_row(json_object_from(config["tools"])["deny"])


def test_heartbeat_and_template_copy_point_at_linkup_skill() -> None:
    from app.services import heartbeat

    heartbeat_text = heartbeat.DEFAULT_HEARTBEAT_INSTRUCTION
    template_heartbeat = Path("agent_template/HEARTBEAT.md").read_text(encoding="utf-8")
    template_source = Path("app/services/template_seeder.py").read_text(encoding="utf-8")

    assert "`web_search`" not in heartbeat_text
    assert "linkup-search" in heartbeat_text
    assert "`web_search`" not in template_heartbeat
    assert "linkup-search" in template_heartbeat
    assert '"web_search"' not in template_source


def test_linkup_api_key_setting_exists() -> None:
    assert "LINKUP_API_KEY" in Settings.model_fields
    assert Settings.model_fields["LINKUP_API_KEY"].default == ""
    assert Settings.model_fields["LINKUP_PROXY_BASE_URL"].default == ""
