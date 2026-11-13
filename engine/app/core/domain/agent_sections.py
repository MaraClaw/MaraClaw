"""Member agent-page sections that a platform admin can hide per company."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

# Canonical order is the display order; the wire contract returns lists in this order.
AGENT_SECTION_IDS: Final[tuple[str, ...]] = (
    "chat",
    "files",
    "skills",
    "tools",
    "mcp",
    "tasks",
    "schedules",
    "activity",
    "channels",
    "people",
    "access",
    "control",
    "vault",
    "pages",
    "browser",
    "settings",
)

# Hidden for a company that has never saved a choice; an explicitly saved list (even ``[]``) replaces it.
DEFAULT_HIDDEN_AGENT_SECTIONS: Final[tuple[str, ...]] = (
    "files",
    "channels",
    "people",
    "access",
    "control",
    "vault",
    "pages",
    "browser",
)


def canonical_agent_sections(values: Iterable[object]) -> list[str]:
    """Keep only known section ids, deduplicated, in canonical order."""
    requested = {value for value in values if isinstance(value, str)}
    return [section for section in AGENT_SECTION_IDS if section in requested]
