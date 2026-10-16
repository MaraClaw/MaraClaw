"""Executable startup contracts against an in-memory catalog/assignment store."""

import importlib
import sys
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict
from uuid import UUID, uuid4

import pytest

from app.records.tool import ToolRecord
from app.services import tool_seeder


class SeedStore:
    def __init__(self) -> None:
        self.tools: dict[str, ToolRecord] = {}
        self.agents = [uuid4(), uuid4()]
        self.assignments: set[tuple[UUID, UUID]] = set()

    async def get_by_name(self, name: str) -> ToolRecord | None:
        assert name != "send_web_message", "fresh startup must not inspect old names"
        return self.tools.get(name)

    async def create(self, *, obj_in: Mapping[str, object]) -> ToolRecord:
        row = ToolRecord.from_row(
            {
                "id": uuid4(),
                "mcp_server_url": None,
                "mcp_server_name": None,
                "mcp_tool_name": None,
                **deepcopy(dict(obj_in)),
            }
        )
        assert row.name not in self.tools
        self.tools[row.name] = row
        return row

    async def update(self, *, db_obj: ToolRecord, obj_in: Mapping[str, object]) -> ToolRecord:
        assert "name" not in obj_in, "startup must not rename persisted tools"
        for key, value in obj_in.items():
            setattr(db_obj, key, deepcopy(value))
        return db_obj

    async def list_all_ids(self) -> list[UUID]:
        return self.agents

    async def ensure_enabled(self, agent_id: UUID, tool_id: UUID) -> bool:
        assignment = (agent_id, tool_id)
        created = assignment not in self.assignments
        self.assignments.add(assignment)
        return created

    async def list_ids_by_names(self, names: list[str]) -> list[UUID]:
        return [self.tools[name].id for name in names if name in self.tools]

    async def list_by_names(self, names: list[str]) -> list[ToolRecord]:
        return [self.tools[name] for name in names if name in self.tools]

    async def list_agent_ids_with_enabled_tools(self, tool_ids: list[UUID]) -> list[UUID]:
        return sorted({agent for agent, tool in self.assignments if tool in tool_ids})


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> SeedStore:
    state = SeedStore()

    @asynccontextmanager
    async def connection() -> AsyncGenerator[None]:
        yield

    monkeypatch.setattr(tool_seeder, "connection_ctx", connection)
    monkeypatch.setattr(tool_seeder, "tool_dao", state)
    monkeypatch.setattr(tool_seeder, "agent_dao", state)
    monkeypatch.setattr(tool_seeder, "agent_tool_dao", state)
    return state


def canonical_tools() -> dict[str, tool_seeder.BuiltinToolDefinition]:
    tools: dict[str, tool_seeder.BuiltinToolDefinition] = {}
    for definition in tool_seeder.BUILTIN_TOOLS:
        assert tool_seeder._is_builtin_tool_definition(definition)
        tools[definition["name"]] = definition
    return tools


async def test_fresh_seed_creates_canonical_tools_and_assigns_defaults(store: SeedStore) -> None:
    # Given: an empty catalog and two existing agents.
    expected = canonical_tools()
    # When: the real startup seeder executes.
    await tool_seeder.seed_builtin_tools()
    # Then: current metadata/config is created without tenant-owned credentials.
    assert set(store.tools) == set(expected)
    assert "send_platform_message" in store.tools
    assert "send_web_message" not in store.tools
    for name, definition in expected.items():
        row = store.tools[name]
        assert row.source == row.type == "builtin"
        assert row.tenant_id is None
        for key in ("display_name", "category", "description", "icon", "parameters_schema", "config_schema", "config"):
            assert getattr(row, key) == definition[key]
    initial_defaults: dict[str, bool] = {}
    for definition in tool_seeder.BUILTIN_TOOLS:
        assert tool_seeder._is_builtin_tool_definition(definition)
        initial_defaults.setdefault(definition["name"], definition["is_default"])
    assert {name: row.is_default for name, row in store.tools.items()} == initial_defaults
    default_ids = {row.id for row in store.tools.values() if row.is_default}
    assert store.assignments == {(agent, tool) for agent in store.agents for tool in default_ids}


async def test_current_seed_rerun_preserves_ids_configs_and_assignments(store: SeedStore) -> None:
    # Given: a fresh seed already applied.
    await tool_seeder.seed_builtin_tools()
    before = {name: asdict(row) for name, row in store.tools.items()}
    assignments = store.assignments.copy()
    # When: startup runs again.
    await tool_seeder.seed_builtin_tools()
    # Then: no duplicate tools/grants or changed defaults.
    assert {name: asdict(row) for name, row in store.tools.items()} == before
    assert store.assignments == assignments


async def test_seed_updates_current_metadata_and_fills_missing_config_defaults(store: SeedStore) -> None:
    # Given: an existing current tool with explicit operational config.
    await tool_seeder.seed_builtin_tools()
    row = store.tools["search_x"]
    row.description = "stale metadata"
    row.config_schema = {}
    row.parameters_schema = {}
    row.is_default = False
    row.config = {"model": "operator-selected-model"}
    store.assignments = {(agent, tool) for agent, tool in store.assignments if tool != row.id}
    # When: the current catalog is reconciled.
    await tool_seeder.seed_builtin_tools()
    # Then: metadata/default assignment is current; explicit values win.
    expected = canonical_tools()["search_x"]
    assert row.description == expected["description"]
    assert row.config_schema == expected["config_schema"]
    assert row.parameters_schema == expected["parameters_schema"]
    assert row.is_default
    assert row.config == {**expected["config"], "model": "operator-selected-model"}
    assert all((agent, row.id) in store.assignments for agent in store.agents)


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("generate_image_siliconflow", "black-forest-labs/FLUX.1-schnell"),
        ("generate_image_openai", "dall-e-3"),
        ("generate_image_google", "gemini-2.5-flash-image"),
    ],
)
async def test_seed_preserves_explicit_image_model(store: SeedStore, name: str, model: str) -> None:
    # Given: an explicitly configured current provider tool.
    await tool_seeder.seed_builtin_tools()
    config = {"model": model, "api_key": "", "base_url": ""}
    store.tools[name].config = config.copy()
    # When: startup reconciles the catalog.
    await tool_seeder.seed_builtin_tools()
    # Then: model preferences are not treated as an upgrade marker.
    assert store.tools[name].config == config


@pytest.mark.parametrize(
    ("anchor", "helpers"),
    [
        (
            "agentbay_computer_click",
            [
                "agentbay_computer_precision_screenshot",
                "agentbay_computer_save_screenshot",
                "agentbay_computer_list_windows",
                "agentbay_computer_close_window",
                "agentbay_computer_dismiss_dialog",
            ],
        ),
        ("agentbay_browser_navigate", ["agentbay_browser_save_screenshot"]),
        ("agentbay_code_execute", ["agentbay_code_write_file", "agentbay_code_read_file", "agentbay_code_edit_file"]),
    ],
)
async def test_seed_assigns_helpers_only_to_enabled_anchor_agents(
    store: SeedStore, anchor: str, helpers: list[str]
) -> None:
    # Given: only one agent has enabled the optional family.
    await tool_seeder.seed_builtin_tools()
    owner, other = store.agents
    store.assignments.add((owner, store.tools[anchor].id))
    # When: startup applies current helper assignment policy.
    await tool_seeder.seed_builtin_tools()
    # Then: helpers remain scoped to enabled users of the family.
    for name in helpers:
        assert (owner, store.tools[name].id) in store.assignments
        assert (other, store.tools[name].id) not in store.assignments


def test_seeder_import_does_not_require_legacy_tenant_config_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: migration-only tenant config APIs are unavailable.
    monkeypatch.setitem(sys.modules, "app.services.tool_config", None)
    # When: startup orchestration is imported again.
    importlib.reload(tool_seeder)
    # Then: fresh startup has no migration dependency on tenant config APIs.
    assert callable(tool_seeder.seed_builtin_tools)
    assert not hasattr(tool_seeder, "tenant_dao")
