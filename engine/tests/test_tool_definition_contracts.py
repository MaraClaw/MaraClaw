from collections.abc import Sequence
from copy import deepcopy

import pytest
from pydantic import TypeAdapter, ValidationError

from app.services import tool_definitions, tool_seeder
from app.services.llm.finish import FINISH_TOOL_DEFINITION, FINISH_TOOL_SEED
from app.services.tool_definition_types import (
    BuiltinToolConfigField,
    BuiltinToolDefinition,
    builtin_tool_config_fields,
)
from app.services.tool_definitions.agentbay import AGENTBAY_TOOLS
from app.services.tool_definitions.aware import AWARE_TOOLS
from app.services.tool_definitions.builtin import BUILTIN_TOOLS
from app.services.tool_definitions.code import CODE_TOOLS
from app.services.tool_definitions.communication import COMMUNICATION_TOOLS
from app.services.tool_definitions.deploy import DEPLOY_BUILTIN_TOOLS
from app.services.tool_definitions.email import EMAIL_TOOLS
from app.services.tool_definitions.feishu_tools import FEISHU_SEED_TOOLS
from app.services.tool_definitions.file import FILE_TOOLS
from app.services.tool_definitions.mcp import MCP_TOOLS
from app.services.tool_definitions.media import MEDIA_TOOLS
from app.services.tool_definitions.okr import OKR_BUILTIN_TOOLS
from app.services.tool_definitions.okr_inline import OKR_INLINE_TOOLS
from app.services.tool_definitions.okr_stray import OKR_STRAY_TOOLS
from app.services.tool_definitions.pages import PAGES_TOOLS
from app.services.tool_definitions.search import SEARCH_TOOLS
from app.services.tool_definitions.skills import SKILL_TOOLS
from app.services.tool_definitions.social import SOCIAL_TOOLS

FAMILY_TABLES: tuple[list[BuiltinToolDefinition], ...] = (
    FILE_TOOLS,
    AWARE_TOOLS,
    COMMUNICATION_TOOLS,
    SEARCH_TOOLS,
    SOCIAL_TOOLS,
    CODE_TOOLS,
    MEDIA_TOOLS,
    MCP_TOOLS,
    EMAIL_TOOLS,
    OKR_INLINE_TOOLS,
    FEISHU_SEED_TOOLS,
    PAGES_TOOLS,
    SKILL_TOOLS,
    OKR_STRAY_TOOLS,
    AGENTBAY_TOOLS,
    OKR_BUILTIN_TOOLS,
    DEPLOY_BUILTIN_TOOLS,
)
PRODUCER_TABLES: tuple[list[BuiltinToolDefinition], ...] = (BUILTIN_TOOLS, *FAMILY_TABLES, [FINISH_TOOL_SEED])


@pytest.mark.parametrize("table", PRODUCER_TABLES)
def test_seed_envelopes_validate_when_loaded_from_each_producer(table: list[BuiltinToolDefinition]) -> None:
    before = deepcopy(table)

    validated = TypeAdapter(list[BuiltinToolDefinition]).validate_python(table)

    assert [(row["name"], row["is_default"], row["parameters_schema"], row["config"]) for row in validated] == [
        (row["name"], row["is_default"], row["parameters_schema"], row["config"]) for row in before
    ]
    assert table == before


@pytest.mark.parametrize("key", sorted(BuiltinToolDefinition.__required_keys__))
def test_seed_envelope_rejects_input_when_a_required_key_is_missing(key: str) -> None:
    seed: dict[str, object] = dict(FINISH_TOOL_SEED)
    del seed[key]

    with pytest.raises(ValidationError):
        TypeAdapter(BuiltinToolDefinition).validate_python(seed)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("name", []),
        ("display_name", {}),
        ("description", []),
        ("category", []),
        ("icon", []),
        ("is_default", {}),
        ("parameters_schema", []),
        ("config", []),
        ("config_schema", []),
    ],
)
def test_seed_envelope_rejects_input_when_a_field_has_an_incompatible_type(key: str, value: object) -> None:
    seed: dict[str, object] = {**FINISH_TOOL_SEED, key: value}

    with pytest.raises(ValidationError):
        TypeAdapter(BuiltinToolDefinition).validate_python(seed)


def test_catalog_composition_retains_original_rows_when_families_are_combined() -> None:
    expected = [FINISH_TOOL_SEED, *(row for table in FAMILY_TABLES for row in table)]

    composed = BUILTIN_TOOLS

    assert all(row is original for row, original in zip(composed, expected, strict=True))
    assert FINISH_TOOL_SEED["parameters_schema"] is FINISH_TOOL_DEFINITION["function"]["parameters"]


def test_public_tables_retain_identity_when_imported_through_the_seeder() -> None:
    expected = (BUILTIN_TOOLS, AGENTBAY_TOOLS, DEPLOY_BUILTIN_TOOLS, OKR_BUILTIN_TOOLS)

    public_tables: tuple[Sequence[BuiltinToolDefinition], ...] = (
        tool_seeder.BUILTIN_TOOLS,
        tool_seeder.AGENTBAY_TOOLS,
        tool_seeder.DEPLOY_BUILTIN_TOOLS,
        tool_seeder.OKR_BUILTIN_TOOLS,
        tool_definitions.BUILTIN_TOOLS,
        tool_definitions.AGENTBAY_TOOLS,
        tool_definitions.DEPLOY_BUILTIN_TOOLS,
        tool_definitions.OKR_BUILTIN_TOOLS,
    )

    assert all(table is original for table, original in zip(public_tables, expected * 2, strict=True))
    assert tool_seeder.BuiltinToolDefinition is BuiltinToolDefinition


def test_config_fields_keep_dynamic_metadata_when_building_the_schema_list() -> None:
    field: BuiltinToolConfigField = {
        "key": "provider",
        "type": "select",
        "default": "custom",
        "vendor_extension": {"tiers": [{"priority": 2, "enabled": True, "limit": None}]},
    }
    before = deepcopy(field)

    fields = builtin_tool_config_fields(field)

    assert fields[0] is field
    assert field == before
