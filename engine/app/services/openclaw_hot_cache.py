"""Process-local TTLs for the OpenClaw send/poll hot path.

Redis is unused here on purpose: these values are cheap to rebuild, and a
miss must never block enqueue or poll.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any
from uuid import UUID

from app.records.agent import AgentRecord
from app.services.llm.turn import ModelBundle

_DEFAULT_TTL_SECONDS = 45.0
_LAST_SEEN_TTL_SECONDS = 20.0
_store: dict[str, tuple[float, Any]] = {}


def _now() -> float:
    return time.monotonic()


def cache_get(key: str) -> Any | None:
    item = _store.get(key)
    if item is None:
        return None
    expires, value = item
    if _now() >= expires:
        _store.pop(key, None)
        return None
    return value


def cache_set(key: str, value: Any, *, ttl: float = _DEFAULT_TTL_SECONDS) -> None:
    if ttl <= 0:
        return
    _store[key] = (_now() + ttl, value)


def cache_drop(key: str) -> None:
    _store.pop(key, None)


def drop_model_caches() -> None:
    """Drop ensured/bundle entries after a company model or subscription change."""
    for key in list(_store):
        if key.startswith("ensured:") or key.startswith("bundle:"):
            _store.pop(key, None)


def drop_cached_agent(agent_id: UUID) -> None:
    """Drop API-key lookups for this agent (key rotate / disable)."""
    for key, item in list(_store.items()):
        if not key.startswith("ockey:"):
            continue
        value = item[1]
        if isinstance(value, AgentRecord) and value.id == agent_id:
            _store.pop(key, None)


def reset() -> None:
    """Test helper."""
    _store.clear()


def _slot_key(agent: AgentRecord) -> str:
    return (
        f"{agent.id}:"
        f"{getattr(agent, 'primary_model_id', None)}:"
        f"{getattr(agent, 'secondary_model_id', None)}:"
        f"{getattr(agent, 'fallback_model_id', None)}"
    )


def recently_ensured(agent: AgentRecord) -> bool:
    return cache_get(f"ensured:{_slot_key(agent)}") is True


def mark_ensured(agent: AgentRecord) -> None:
    cache_set(f"ensured:{_slot_key(agent)}", True)


def get_cached_bundle(agent: AgentRecord) -> ModelBundle | None:
    value = cache_get(f"bundle:{_slot_key(agent)}")
    return value if isinstance(value, ModelBundle) else None


def set_cached_bundle(agent: AgentRecord, bundle: ModelBundle) -> None:
    cache_set(f"bundle:{_slot_key(agent)}", bundle)


def api_key_cache_key(api_key: str) -> str:
    digest = hashlib.sha256(api_key.encode()).hexdigest()[:24]
    return f"ockey:{digest}"


def get_cached_agent_by_key(api_key: str) -> AgentRecord | None:
    value = cache_get(api_key_cache_key(api_key))
    return value if isinstance(value, AgentRecord) else None


def set_cached_agent_by_key(api_key: str, agent: AgentRecord) -> None:
    cache_set(api_key_cache_key(api_key), agent, ttl=30.0)


def display_name_key(user_id: UUID) -> str:
    return f"dname:{user_id}"


def get_cached_display_name(user_id: UUID) -> str | None:
    value = cache_get(display_name_key(user_id))
    return value if isinstance(value, str) else None


def set_cached_display_name(user_id: UUID, name: str) -> None:
    cache_set(display_name_key(user_id), name, ttl=60.0)


def should_touch_last_seen(agent_id: UUID) -> bool:
    key = f"seen:{agent_id}"
    if cache_get(key) is True:
        return False
    cache_set(key, True, ttl=_LAST_SEEN_TTL_SECONDS)
    return True
