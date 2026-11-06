from pathlib import Path
from typing import Final, NotRequired, TypedDict

from pydantic import TypeAdapter

from app.core.json_types import JsonObject, is_json_object, json_loads_object, json_object_from, str_list_from_row


class _Entries(TypedDict):
    entries: NotRequired[dict[str, JsonObject]]


class _SkillLoad(TypedDict):
    extraDirs: NotRequired[list[str]]


class _Skills(_Entries):
    load: NotRequired[_SkillLoad]


class _Mcp(TypedDict):
    servers: NotRequired[dict[str, JsonObject]]


class _Agents(_Entries):
    defaults: NotRequired[JsonObject]


class _GuestConfig(TypedDict):
    agents: NotRequired[_Agents]
    skills: NotRequired[_Skills]
    plugins: NotRequired[_Entries]
    mcp: NotRequired[_Mcp]


_GUEST_CONFIG = TypeAdapter(_GuestConfig)

# Paths the generator emits (or that shadow them). A guest value at one of these is stale: it is dropped so the
# generated value, or its absence (a retired provider's alias, key or auth profile), is authoritative.
_PLATFORM_PATHS: Final[tuple[tuple[str, ...], ...]] = (
    ("env",),
    ("auth",),
    ("models",),
    ("gateway",),
    ("tools",),
    ("hooks",),
    ("memory", "search"),
    ("plugins", "enabled"),
    ("plugins", "slots"),
    ("agents", "defaults", "workspace"),
    ("agents", "defaults", "model"),
    ("agents", "defaults", "models"),
    ("agents", "defaults", "memorySearch"),
    ("agents", "defaults", "heartbeat"),
)
# Guest records merged entry by entry below, with generated entries winning.
_RESOURCE_PATHS: Final[tuple[tuple[str, ...], ...]] = (
    ("mcp", "servers"),
    ("skills", "entries"),
    ("plugins", "entries"),
    ("skills", "load", "extraDirs"),
)


def _drop(config: JsonObject, path: tuple[str, ...]) -> None:
    head, *rest = path
    child = config.get(head)
    if not rest:
        _ = config.pop(head, None)
    elif is_json_object(child):
        _drop(child, tuple(rest))
        if not child:
            del config[head]


def _guest_owned(guest: JsonObject) -> JsonObject:
    """Strip platform and resource paths from a parsed guest config; the remainder is guest-owned."""
    for path in (*_PLATFORM_PATHS, *_RESOURCE_PATHS):
        _drop(guest, path)
    for entry in json_object_from(json_object_from(guest.get("agents")).get("entries")).values():
        if is_json_object(entry):
            _ = entry.pop("model", None)
    return guest


def _merge(guest: JsonObject, platform: JsonObject) -> JsonObject:
    merged = guest.copy()
    for key, value in platform.items():
        current = merged.get(key)
        merged[key] = _merge(current, value) if is_json_object(current) and is_json_object(value) else value
    return merged


def _normalized_skill_dir(directory: str) -> str:
    for state_root in ("/home/node/.openclaw", "/home/node/custom-state"):
        if directory != "/home/node/.openclaw/skills" and (
            directory == state_root or directory.startswith(f"{state_root}/")
        ):
            return "${OPENCLAW_STATE_DIR}" + directory[len(state_root) :]
    return directory


def preserve_guest_resources(path: Path, generated: JsonObject) -> JsonObject:
    """Retain guest-owned config; generated platform records always win."""
    stored = path.read_bytes() if path.exists() else b"{}"
    previous = _GUEST_CONFIG.validate_json(stored, strict=True)
    result = _merge(_guest_owned(json_loads_object(stored)), generated)
    for section, key, entries in (
        ("mcp", "servers", previous.get("mcp", {}).get("servers", {})),
        ("skills", "entries", previous.get("skills", {}).get("entries", {})),
        ("plugins", "entries", previous.get("plugins", {}).get("entries", {})),
    ):
        managed_entries = json_object_from(json_object_from(generated.get(section)).get(key))
        if entries or managed_entries:
            result[section] = {**json_object_from(result.get(section)), key: {**entries, **managed_entries}}

    skill_load = previous.get("skills", {}).get("load", {})
    if "extraDirs" in skill_load:
        skills = json_object_from(result.get("skills"))
        managed_load = json_object_from(skills.get("load"))
        managed_dirs = str_list_from_row(managed_load.get("extraDirs"))
        custom_dirs = [_normalized_skill_dir(directory) for directory in skill_load["extraDirs"]]
        load: JsonObject = {**managed_load, "extraDirs": list(dict.fromkeys([*custom_dirs, *managed_dirs]))}
        result["skills"] = {**skills, "load": load}

    return result
