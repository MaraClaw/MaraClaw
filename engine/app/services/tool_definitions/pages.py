"""Seed rows extracted from builtin.py. Data only."""


from typing import Any

PAGES_TOOLS: list[dict[str, Any]] = [
        {
            "name": "publish_page",
            "display_name": "Publish Page",
            "description": "Publish an HTML file from workspace as a public page. Returns a public URL that anyone can access without login. Only .html/.htm files can be published.",
            "category": "pages",
            "icon": "🌐",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path in workspace, e.g. 'workspace/output.html'"},
                },
                "required": ["path"],
            },
            "config": {},
            "config_schema": {},
        },
        {
            "name": "list_published_pages",
            "display_name": "List Published Pages",
            "description": "List all pages published by this agent, showing their public URLs and view counts.",
            "category": "pages",
            "icon": "📋",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {},
            },
            "config": {},
            "config_schema": {},
        },
]
