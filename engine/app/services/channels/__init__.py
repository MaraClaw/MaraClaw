"""Unified chat / IM channel package.

Use this package for new channel integrations and for shared helpers when
migrating legacy per-provider modules under ``app.api.*`` / ``app.services.*``.

Legacy connectors (feishu, wecom, slack, …) remain in place; they should
gradually call helpers from here rather than duplicating CRUD / session /
reply loops.
"""

from app.services.channels.types import (
    CHANNEL_TYPES,
    ChannelKind,
    is_known_channel_type,
    normalize_channel_type,
    outbound_provider_key,
)

__all__ = (
    "CHANNEL_TYPES",
    "ChannelKind",
    "is_known_channel_type",
    "normalize_channel_type",
    "outbound_provider_key",
)
