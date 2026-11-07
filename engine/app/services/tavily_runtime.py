"""Canonical configuration keys for guest image-bundled Tavily skills."""

from __future__ import annotations

from typing import Final

TAVILY_SEARCH_SKILL_FOLDER: Final[str] = "tavily-search"
TAVILY_CRAWL_SKILL_FOLDER: Final[str] = "tavily-crawl"


def tavily_default_skill_folder_names() -> list[str]:
    """Return the Tavily skill configuration keys for agents."""
    return [
        TAVILY_SEARCH_SKILL_FOLDER,
        TAVILY_CRAWL_SKILL_FOLDER,
    ]
