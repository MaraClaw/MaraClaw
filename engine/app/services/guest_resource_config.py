from pathlib import Path
from typing import NotRequired, TypedDict

from pydantic import TypeAdapter

from app.core.json_types import JsonObject, json_object_from, str_list_from_row


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
        managed = json_object_from(generated.get(section))
        managed_entries = json_object_from(managed.get(key))
        if entries or managed_entries:
            result[section] = {**managed, key: {**entries, **managed_entries}}

    skill_load = previous.get("skills", {}).get("load", {})
    if "extraDirs" in skill_load:
        skills = json_object_from(result.get("skills"))
        managed_load = json_object_from(skills.get("load"))
        managed_dirs = str_list_from_row(managed_load.get("extraDirs"))
        custom_dirs: list[str] = []
        for directory in skill_load["extraDirs"]:
            for state_root in ("/home/node/.openclaw", "/home/node/custom-state"):
                if directory != "/home/node/.openclaw/skills" and (
                    directory == state_root or directory.startswith(f"{state_root}/")
                ):
                    directory = "${OPENCLAW_STATE_DIR}" + directory[len(state_root) :]
                    break
            custom_dirs.append(directory)
        load: JsonObject = {
            **managed_load,
            "extraDirs": list(dict.fromkeys([*custom_dirs, *managed_dirs])),
        }
        result["skills"] = {**skills, "load": load}

    return result
