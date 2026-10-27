"""Canonical configuration keys for Photon-bundled Linkup skills."""

from __future__ import annotations

from typing import Final

LINKUP_SEARCH_SKILL_FOLDER: Final[str] = "linkup-search"
LINKUP_FETCH_SKILL_FOLDER: Final[str] = "linkup-fetch"


def linkup_default_skill_folder_names() -> list[str]:
    """Return the Linkup skill configuration keys for agents."""
    return [
        LINKUP_SEARCH_SKILL_FOLDER,
        LINKUP_FETCH_SKILL_FOLDER,
    ]
