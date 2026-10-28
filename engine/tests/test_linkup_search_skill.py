"""Gating tests for Linkup configuration and removal of built-in search-engine tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.config import Settings
from app.core.json_types import json_as_str_or, json_object_from, str_list_from_row
from app.records.agent import AgentRecord
from app.services import agent_manager as agent_manager_module
from app.services.agent_manager import AgentManager
from app.services.agent_tool_exec import registry
from app.services.agent_tools_definitions import AGENT_TOOLS
from app.services.linkup.tokens import parse_proxy_token
from app.services.linkup_runtime import (
    LINKUP_FETCH_SKILL_FOLDER,
    LINKUP_SEARCH_SKILL_FOLDER,
    linkup_default_skill_folder_names,
)
from app.services.tool_definitions import BUILTIN_TOOLS
from app.services.tool_seeder import SYNC_IS_DEFAULT_TOOL_NAMES

DEFAULT_LINKUP_SKILLS = (
    LINKUP_SEARCH_SKILL_FOLDER,
    LINKUP_FETCH_SKILL_FOLDER,
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


def test_linkup_defaults_are_available_without_local_skill_directories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda _path: False)

    assert linkup_default_skill_folder_names() == list(DEFAULT_LINKUP_SKILLS)


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
