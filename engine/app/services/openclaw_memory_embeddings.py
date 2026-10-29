"""Optional OpenAI-compatible memory embeddings for OpenClaw guests.

The four ``EMBEDDING_*`` settings are all-or-nothing: only a complete, non-blank set selects the custom endpoint;
anything less leaves OpenClaw's own memory-search defaults in charge.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from app.core.json_types import JsonObject


class EmbeddingSettings(Protocol):
    EMBEDDING_MODEL: str
    EMBEDDING_DIMS: str
    EMBEDDING_PROVIDER_URL: str
    EMBEDDING_API_KEY: str


class MemoryEmbeddingConfigError(ValueError):
    """A complete embedding configuration is unusable. Messages never carry setting values."""


@dataclass(frozen=True, slots=True)
class MemoryEmbeddingConfig:
    model: str
    dimensions: int
    base_url: str
    api_key: str


def memory_embedding_config(source: EmbeddingSettings) -> MemoryEmbeddingConfig | None:
    """Return the effective custom embedding config, or ``None`` when any setting is missing or blank."""
    model = source.EMBEDDING_MODEL.strip()
    dimensions = source.EMBEDDING_DIMS.strip()
    base_url = source.EMBEDDING_PROVIDER_URL.strip()
    api_key = source.EMBEDDING_API_KEY.strip()
    if not (model and dimensions and base_url and api_key):
        return None
    if not (dimensions.isascii() and dimensions.isdecimal() and int(dimensions) > 0):
        raise MemoryEmbeddingConfigError("EMBEDDING_DIMS must be a positive integer")
    return MemoryEmbeddingConfig(model=model, dimensions=int(dimensions), base_url=base_url, api_key=api_key)


def memory_search_block(config: MemoryEmbeddingConfig | None) -> JsonObject:
    """Build ``memory.search``; without a custom config the provider stays unset (OpenClaw default)."""
    if config is None:
        return {"enabled": True}
    return {
        "enabled": True,
        "provider": "openai-compatible",
        "model": config.model,
        "outputDimensionality": config.dimensions,
        "remote": {"baseUrl": config.base_url, "apiKey": config.api_key},
    }


def memory_embedding_fingerprint(config: MemoryEmbeddingConfig | None) -> str:
    """Hash every field so changes and key rotation are detected without exposing values."""
    if config is None:
        return "embed:"
    payload = json.dumps([config.model, config.dimensions, config.base_url, config.api_key])
    return f"embed:{hashlib.sha256(payload.encode()).hexdigest()[:16]}"
