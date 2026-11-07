"""Gating tests for Tavily configuration and removal of built-in search-engine tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.config import Settings, get_settings
from app.core.json_types import json_as_str_or, json_object_from, str_list_from_row
from app.records.agent import AgentRecord
from app.services import agent_manager as agent_manager_module
from app.services.agent_manager import AgentManager
from app.services.agent_tool_exec import registry
from app.services.agent_tools_definitions import AGENT_TOOLS
from app.services.tavily.tokens import parse_proxy_token
from app.services.tavily_runtime import (
    TAVILY_CRAWL_SKILL_FOLDER,
    TAVILY_SEARCH_SKILL_FOLDER,
    tavily_default_skill_folder_names,
)
from app.services.tool_definitions import BUILTIN_TOOLS
from app.services.tool_seeder import SYNC_IS_DEFAULT_TOOL_NAMES

DEFAULT_TAVILY_SKILLS = (
    TAVILY_SEARCH_SKILL_FOLDER,
    TAVILY_CRAWL_SKILL_FOLDER,
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


def test_tavily_defaults_are_available_without_local_skill_directories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda _path: False)

    assert tavily_default_skill_folder_names() == list(DEFAULT_TAVILY_SKILLS)


@pytest.mark.parametrize("upstream_key", ["", "lk_raw_must_not_leak"])
@pytest.mark.parametrize(
    ("proxy_base", "expected_base"),
    [
        ("", "https://deployed-engine.example/guest/api/tavily"),
        (" \t", "https://deployed-engine.example/guest/api/tavily"),
        (" https://dedicated-proxy.example/tavily/// ", "https://dedicated-proxy.example/tavily"),
    ],
)
def test_generate_openclaw_config_enables_authenticated_tavily_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, upstream_key: str, proxy_base: str, expected_base: str
) -> None:
    monkeypatch.setattr(agent_manager_module, "settings", get_settings())
    monkeypatch.setattr(agent_manager_module.settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setattr(agent_manager_module.settings, "TAVILY_API_KEY", upstream_key)
    monkeypatch.setattr(agent_manager_module.settings, "TAVILY_PROXY_ENABLED", True)
    monkeypatch.setattr(agent_manager_module.settings, "TAVILY_PROXY_BASE_URL", proxy_base)
    monkeypatch.setattr(
        agent_manager_module.settings, "OPENCLAW_ENGINE_BASE_URL", "https://deployed-engine.example/guest/"
    )
    monkeypatch.setattr(agent_manager_module.settings, "SECRET_KEY", "cfg-secret")
    manager = AgentManager.__new__(AgentManager)
    agent = AgentRecord(id=uuid4(), name="Search Agent", creator_id=uuid4())

    config = manager._generate_openclaw_config(agent, model=None)

    assert "lk_raw_must_not_leak" not in json.dumps(config)
    skills = json_object_from(json_object_from(config["skills"])["entries"])
    assert set(skills) == set(DEFAULT_TAVILY_SKILLS)
    for folder_name in DEFAULT_TAVILY_SKILLS:
        entry = json_object_from(skills[folder_name])
        assert entry["enabled"] is True
        env = json_object_from(entry["env"])
        assert set(env) == {"TAVILY_API_BASE", "TAVILY_API_KEY"}
        assert env["TAVILY_API_BASE"] == expected_base
        assert parse_proxy_token(json_as_str_or(env["TAVILY_API_KEY"]), secret="cfg-secret") == agent.id
    tools = json_object_from(config["tools"])
    assert "web_search" in str_list_from_row(tools["deny"])
    assert json_object_from(json_object_from(tools["web"])["search"])["enabled"] is False


def test_generate_openclaw_config_disables_tavily_without_raw_key_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(agent_manager_module, "settings", get_settings())
    monkeypatch.setattr(agent_manager_module.settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setattr(agent_manager_module.settings, "TAVILY_API_KEY", "lk_raw_must_not_leak")
    monkeypatch.setattr(agent_manager_module.settings, "TAVILY_PROXY_ENABLED", False)
    monkeypatch.setattr(
        agent_manager_module.settings,
        "TAVILY_PROXY_BASE_URL",
        "http://maraclaw-engine:8000/api/tavily",
    )
    manager = AgentManager.__new__(AgentManager)
    agent = AgentRecord(id=uuid4(), name="Search Agent", creator_id=uuid4())

    config = manager._generate_openclaw_config(agent, model=None)

    assert "lk_raw_must_not_leak" not in json.dumps(config)
    skills = json_object_from(json_object_from(config["skills"])["entries"])
    assert set(skills) == set(DEFAULT_TAVILY_SKILLS)
    for folder_name in DEFAULT_TAVILY_SKILLS:
        assert skills[folder_name] == {"enabled": False, "env": {}}
    assert "web_search" in str_list_from_row(json_object_from(config["tools"])["deny"])


async def test_workspace_copies_heartbeat_template_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import agent_workspace

    monkeypatch.setattr(agent_workspace.user_dao, "get", AsyncMock(return_value=None))
    template_dir = Path("agent_template")
    agent = AgentRecord(id=uuid4(), name="Search Agent", creator_id=uuid4())

    files = await agent_workspace.render_agent_files(agent, template_dir)

    assert files["HEARTBEAT.md"] == (template_dir / "HEARTBEAT.md").read_bytes()


def test_tavily_api_key_setting_exists() -> None:
    assert "TAVILY_API_KEY" in Settings.model_fields
    assert Settings.model_fields["TAVILY_API_KEY"].default == ""
    assert Settings.model_fields["TAVILY_PROXY_BASE_URL"].default == ""
