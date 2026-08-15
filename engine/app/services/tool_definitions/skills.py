"""Seed rows extracted from builtin.py. Data only."""


from typing import Any

SKILL_TOOLS: list[dict[str, Any]] = [
        {
            "name": "search_clawhub",
            "display_name": "Search ClawHub",
            "description": "Search the ClawHub skill registry for skills matching a query. Returns a list of available skills with name, description, and last updated date.",
            "category": "discovery",
            "icon": "🔎",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query, e.g. 'research', 'code review', 'market analysis'",
                    },
                },
                "required": ["query"],
            },
            "config": {},
            "config_schema": {},
        },
        {
            "name": "install_skill",
            "display_name": "Install Skill",
            "description": "Install a skill into this agent's workspace. Accepts a ClawHub slug (e.g. 'market-research') or a GitHub URL.",
            "category": "discovery",
            "icon": "📥",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "ClawHub skill slug (e.g. 'market-research') or GitHub URL",
                    },
                },
                "required": ["source"],
            },
            "config": {},
            "config_schema": {},
        },
]
