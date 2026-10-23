from pathlib import Path
from typing import NotRequired, TypedDict

from pydantic import TypeAdapter

from app.core.json_types import JsonObject, json_object_from


class _Entries(TypedDict):
    entries: NotRequired[dict[str, JsonObject]]


class _SkillLoad(TypedDict):
    extraDirs: NotRequired[list[str]]


class _Skills(_Entries):
    load: NotRequired[_SkillLoad]


class _Mcp(TypedDict):
    servers: NotRequired[dict[str, JsonObject]]


class _Resources(TypedDict):
    skills: NotRequired[_Skills]
    plugins: NotRequired[_Entries]
    mcp: NotRequired[_Mcp]


_RESOURCE_CONFIG = TypeAdapter(_Resources)


def preserve_guest_resources(path: Path, generated: JsonObject) -> JsonObject:
    """Retain guest resources; generated platform records always win."""
    previous: _Resources = _RESOURCE_CONFIG.validate_json(path.read_bytes(), strict=True) if path.exists() else {}
    result = generated.copy()
    for section, key, entries in (
        ("mcp", "servers", previous.get("mcp", {}).get("servers", {})),
        ("skills", "entries", previous.get("skills", {}).get("entries", {})),
        ("plugins", "entries", previous.get("plugins", {}).get("entries", {})),
    ):
        retained: JsonObject = {
            name: value for name, value in entries.items() if section != "plugins" or name != "memory-tencentdb"
        }
        managed = json_object_from(generated.get(section))
        managed_entries = json_object_from(managed.get(key))
        if retained or managed_entries:
            result[section] = {**managed, key: {**retained, **managed_entries}}

    skill_load = previous.get("skills", {}).get("load", {})
    if "extraDirs" in skill_load:
        skills = json_object_from(result.get("skills"))
        load: JsonObject = {"extraDirs": [*skill_load["extraDirs"]], **json_object_from(skills.get("load"))}
        result["skills"] = {**skills, "load": load}

    if (
        "memory-tencentdb" in previous.get("plugins", {}).get("entries", {})
        or (path.parent / "extensions" / "memory-tencentdb").exists()
    ):
        # OpenClaw auto-discovers persisted extensions; retain a named tombstone without deleting their data.
        plugins = json_object_from(result.get("plugins"))
        entries = json_object_from(plugins.get("entries"))
        result["plugins"] = {**plugins, "entries": {**entries, "memory-tencentdb": {"enabled": False}}}
    return result
