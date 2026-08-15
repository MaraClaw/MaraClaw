"""Gating tests for Linkup skill seed and removal of built-in search-engine tools."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.config import Settings
from app.services import agent_manager as agent_manager_module
from app.services import linkup_runtime
from app.services.agent_manager import AgentManager
from app.services.agent_tool_exec import registry
from app.services.agent_tools_definitions import AGENT_TOOLS
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

DEFAULT_LINKUP_SKILLS = (
    LINKUP_SEARCH_SKILL_FOLDER,
    LINKUP_FETCH_SKILL_FOLDER,
    LINKUP_RESEARCH_SKILL_FOLDER,
    LINKUP_EXTRACT_SKILL_FOLDER,
)
from app.services.tool_definitions import BUILTIN_TOOLS
from app.services.tool_seeder import SYNC_IS_DEFAULT_TOOL_NAMES

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
    catalog_names = {tool["function"]["name"] for tool in AGENT_TOOLS}

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


def test_official_linkup_search_skill_payload_teaches_rest_search() -> None:
    # Given: the vendored official Linkup skill package used by the seeder.
    manifest = load_linkup_manifest()
    skill_md = linkup_skill_root() / LINKUP_SEARCH_SKILL_FOLDER / "SKILL.md"
    skill_text = skill_md.read_text(encoding="utf-8")

    # Then: official folder/name and REST contract are present.
    assert LINKUP_SEARCH_SKILL_FOLDER in (manifest.get("skills") or [])
    assert LINKUP_SEARCH_SKILL_FOLDER in (manifest.get("default_skills") or [])
    assert LINKUP_SEARCH_SKILL_FOLDER in linkup_skill_folder_names()
    assert LINKUP_SEARCH_SKILL_FOLDER in linkup_default_skill_folder_names()
    assert skill_md.is_file()
    assert "name: linkup-search" in skill_text
    assert "https://api.linkup.so/v1/search" in skill_text
    assert "LINKUP_API_BASE" in skill_text
    assert '"q":' in skill_text
    assert "depth" in skill_text
    assert "standard" in skill_text
    assert "deep" in skill_text
    assert "outputType" in skill_text
    assert "sourcedAnswer" in skill_text


def test_official_linkup_fetch_skill_payload_teaches_rest_fetch() -> None:
    manifest = load_linkup_manifest()
    skill_md = linkup_skill_root() / LINKUP_FETCH_SKILL_FOLDER / "SKILL.md"
    skill_text = skill_md.read_text(encoding="utf-8")

    assert LINKUP_FETCH_SKILL_FOLDER in (manifest.get("skills") or [])
    assert LINKUP_FETCH_SKILL_FOLDER in (manifest.get("default_skills") or [])
    assert LINKUP_FETCH_SKILL_FOLDER in linkup_skill_folder_names()
    assert LINKUP_FETCH_SKILL_FOLDER in linkup_default_skill_folder_names()
    assert skill_md.is_file()
    assert "name: linkup-fetch" in skill_text
    assert "https://api.linkup.so/v1/fetch" in skill_text
    assert "LINKUP_API_BASE" in skill_text
    assert "-X POST" in skill_text
    assert '"url":' in skill_text


def test_official_linkup_research_skill_payload_teaches_rest_research() -> None:
    manifest = load_linkup_manifest()
    skill_md = linkup_skill_root() / LINKUP_RESEARCH_SKILL_FOLDER / "SKILL.md"
    skill_text = skill_md.read_text(encoding="utf-8")

    assert LINKUP_RESEARCH_SKILL_FOLDER in (manifest.get("skills") or [])
    assert LINKUP_RESEARCH_SKILL_FOLDER in (manifest.get("default_skills") or [])
    assert LINKUP_RESEARCH_SKILL_FOLDER in linkup_skill_folder_names()
    assert LINKUP_RESEARCH_SKILL_FOLDER in linkup_default_skill_folder_names()
    assert skill_md.is_file()
    assert "name: linkup-research" in skill_text
    assert "https://api.linkup.so/v1/research" in skill_text
    assert "LINKUP_API_BASE" in skill_text
    assert "mode" in skill_text
    assert "reasoningDepth" in skill_text


def test_official_linkup_extract_skill_payload_teaches_rest_extract() -> None:
    manifest = load_linkup_manifest()
    skill_md = linkup_skill_root() / LINKUP_EXTRACT_SKILL_FOLDER / "SKILL.md"
    skill_text = skill_md.read_text(encoding="utf-8")

    assert LINKUP_EXTRACT_SKILL_FOLDER in (manifest.get("skills") or [])
    assert LINKUP_EXTRACT_SKILL_FOLDER in (manifest.get("default_skills") or [])
    assert LINKUP_EXTRACT_SKILL_FOLDER in linkup_skill_folder_names()
    assert LINKUP_EXTRACT_SKILL_FOLDER in linkup_default_skill_folder_names()
    assert skill_md.is_file()
    assert "name: linkup-extract" in skill_text
    assert "https://api.linkup.so/v1/extract" in skill_text
    assert "LINKUP_API_BASE" in skill_text
    assert '"url":' in skill_text
    assert "schema" in skill_text


@pytest.mark.asyncio
async def test_seed_linkup_skills_upserts_official_default_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the real seeder path with a captured skill_dao.
    upserts: list[dict] = []

    async def fake_get_by_folder_name(_folder_name: str):
        return None

    async def fake_upsert_skill_package(**kwargs):
        upserts.append(kwargs)
        return SimpleNamespace(id=uuid4(), folder_name=kwargs["folder_name"])

    monkeypatch.setattr(linkup_runtime.skill_dao, "get_by_folder_name", fake_get_by_folder_name)
    monkeypatch.setattr(linkup_runtime.skill_dao, "upsert_skill_package", fake_upsert_skill_package)

    # When
    processed = await seed_linkup_skills(None)

    # Then
    assert processed == len(DEFAULT_LINKUP_SKILLS)
    assert {item["folder_name"] for item in upserts} == set(DEFAULT_LINKUP_SKILLS)
    by_folder = {item["folder_name"]: item for item in upserts}
    expected_markers = {
        LINKUP_SEARCH_SKILL_FOLDER: "https://api.linkup.so/v1/search",
        LINKUP_FETCH_SKILL_FOLDER: "https://api.linkup.so/v1/fetch",
        LINKUP_RESEARCH_SKILL_FOLDER: "https://api.linkup.so/v1/research",
        LINKUP_EXTRACT_SKILL_FOLDER: "https://api.linkup.so/v1/extract",
    }
    for folder_name, marker in expected_markers.items():
        package = by_folder[folder_name]
        assert package["name"] == folder_name
        assert package["is_builtin"] is True
        assert package["is_default"] is True
        files = dict(package["files"])
        assert marker in files["SKILL.md"]
        assert any(path.startswith("references/") for path in files)


def test_generate_openclaw_config_enables_linkup_skill_and_denies_native_web_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_API_KEY", "lk_test_key", raising=False)
    monkeypatch.setattr(
        agent_manager_module.settings,
        "LINKUP_PROXY_BASE_URL",
        "http://maraclaw-engine:8000/api/linkup",
        raising=False,
    )
    manager = AgentManager.__new__(AgentManager)
    agent = SimpleNamespace(id=uuid4(), name="Search Agent", creator_id=uuid4(), primary_model_id=None)

    # When: proxy off keeps today's single-key inject
    config = manager._generate_openclaw_config(agent, model=None, linkup_proxy=False)

    # Then
    skills = config["skills"]["entries"]
    assert set(skills) == set(DEFAULT_LINKUP_SKILLS)
    for folder_name in DEFAULT_LINKUP_SKILLS:
        assert skills[folder_name]["enabled"] is True
        assert skills[folder_name]["env"]["LINKUP_API_KEY"] == "lk_test_key"
        assert "LINKUP_API_BASE" not in skills[folder_name]["env"]
    assert "web_search" in config["tools"]["deny"]
    assert config["tools"]["web"]["search"]["enabled"] is False


def test_generate_openclaw_config_proxy_sets_base_and_hides_raw_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_API_KEY", "lk_raw_must_not_leak", raising=False)
    monkeypatch.setattr(
        agent_manager_module.settings,
        "LINKUP_PROXY_BASE_URL",
        "http://maraclaw-engine:8000/api/linkup",
        raising=False,
    )
    monkeypatch.setattr(agent_manager_module.settings, "SECRET_KEY", "cfg-secret", raising=False)
    manager = AgentManager.__new__(AgentManager)
    agent = SimpleNamespace(id=uuid4(), name="Search Agent", creator_id=uuid4(), primary_model_id=None)

    config = manager._generate_openclaw_config(agent, model=None, linkup_proxy=True)
    skills = config["skills"]["entries"]
    for folder_name in DEFAULT_LINKUP_SKILLS:
        env = skills[folder_name]["env"]
        assert env["LINKUP_API_BASE"] == "http://maraclaw-engine:8000/api/linkup"
        assert env["LINKUP_API_KEY"] != "lk_raw_must_not_leak"
        assert "lk_raw_must_not_leak" not in env["LINKUP_API_KEY"]


def test_generate_openclaw_config_omits_empty_linkup_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_API_KEY", "", raising=False)
    manager = AgentManager.__new__(AgentManager)
    agent = SimpleNamespace(id=uuid4(), name="Search Agent", creator_id=uuid4(), primary_model_id=None)

    config = manager._generate_openclaw_config(agent, model=None)

    assert set(config["skills"]["entries"]) == set(DEFAULT_LINKUP_SKILLS)
    for folder_name in DEFAULT_LINKUP_SKILLS:
        assert config["skills"]["entries"][folder_name]["enabled"] is True
        assert config["skills"]["entries"][folder_name]["env"] == {}
    assert "web_search" in config["tools"]["deny"]


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
