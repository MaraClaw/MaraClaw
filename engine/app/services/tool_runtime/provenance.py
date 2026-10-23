"""Out-of-band provenance for merged tool configuration.

Stored JSON is untrusted, so source and endpoint facts travel as typed attributes of the merged mapping instead of
as keys. A copy of the mapping (``dict(config)``, ``{**config}``) drops them and is treated conservatively.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from app.core.json_types import JsonObject, JsonValue, json_as_str_or


class ConfigLayer(StrEnum):
    """Configuration layer that wrote a value."""

    BUILTIN_GLOBAL = "builtin_global"
    CUSTOM_GLOBAL = "custom_global"
    TENANT = "tenant"
    AGENT = "agent"
    UNTRUSTED = "untrusted"


@dataclass(frozen=True, slots=True)
class FieldSource:
    """Layer that wrote a field and the ``api_url`` visible at that layer ("" inherits the consumer's default)."""

    layer: ConfigLayer
    endpoint: str


@dataclass(frozen=True, slots=True)
class ConfigProvenance:
    """Final writer of every merged field, captured before merging and caching."""

    sources: Mapping[str, FieldSource]


class ProvenancedConfig(dict[str, JsonValue]):
    """Merged configuration carrying its provenance; equal to and cached like the plain mapping."""

    __slots__: tuple[str, ...] = ("provenance",)

    def __init__(self, values: JsonObject, provenance: ConfigProvenance) -> None:
        super().__init__(values)
        self.provenance: ConfigProvenance = provenance


def merge_layers(layers: Sequence[tuple[ConfigLayer, JsonObject]]) -> ProvenancedConfig:
    """Merge layers with later layers winning, recording who wrote each field and which endpoint it saw."""
    merged: JsonObject = {}
    sources: dict[str, FieldSource] = {}
    endpoint = ""
    for layer, values in layers:
        if "api_url" in values:
            endpoint = json_as_str_or(values["api_url"])
        merged.update(values)
        sources.update(dict.fromkeys(values, FieldSource(layer, endpoint)))
    return ProvenancedConfig(merged, ConfigProvenance(MappingProxyType(sources)))


def config_provenance(config: object) -> ConfigProvenance | None:
    """Return the provenance of a loader-produced mapping, or ``None`` for any other mapping."""
    return config.provenance if isinstance(config, ProvenancedConfig) else None
