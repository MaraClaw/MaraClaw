"""Seed rows extracted from builtin.py. Data only."""


from typing import Any

SEARCH_TOOLS: list[dict[str, Any]] = [
        {
            "name": "read_webpage",
            "display_name": "Read Webpage",
            "description": "Fetch a public HTTP/HTTPS URL directly and extract readable webpage text. Use this when you already have a specific link and need its page content without relying on an external reader service.",
            "category": "search",
            "icon": "🌐",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full public HTTP/HTTPS URL to read"},
                    "max_chars": {"type": "integer", "description": "Max characters to return (default 12000, max 50000)"},
                    "include_links": {
                        "type": "boolean",
                        "description": "Whether to include extracted page links (default false)",
                    },
                },
                "required": ["url"],
            },
            "config": {},
            "config_schema": {},
        },
        {
            "name": "search_x",
            "display_name": "Search X",
            "description": "Search X (Twitter) posts, users, and threads via xAI X Search. Use for live public conversation, handles, hashtags, and recent posts. Billed as a grok-4.6 Responses call plus X Search. enable_image_understanding and enable_video_understanding add extra cost; leave them false unless needed.",
            "category": "search",
            "icon": "X",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for on X (keywords, a topic, a question, or an @handle).",
                    },
                    "allowed_x_handles": {
                        "type": "string",
                        "description": "Optional comma-separated X handles to include (without @). Max 20. Cannot be combined with excluded_x_handles.",
                    },
                    "excluded_x_handles": {
                        "type": "string",
                        "description": "Optional comma-separated X handles to exclude (without @). Max 20. Cannot be combined with allowed_x_handles.",
                    },
                    "from_date": {
                        "type": "string",
                        "description": "Optional start date (YYYY-MM-DD).",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "Optional end date (YYYY-MM-DD).",
                    },
                    "enable_image_understanding": {
                        "type": "boolean",
                        "description": "Analyze images in matching posts (default false).",
                    },
                    "enable_video_understanding": {
                        "type": "boolean",
                        "description": "Analyze videos in matching posts (default false).",
                    },
                },
                "required": ["query"],
            },
            "config": {
                "model": "",
                "api_key": "",
                "base_url": "",
            },
            "config_schema": {
                "fields": [
                    {
                        "key": "model",
                        "label": "Model",
                        "type": "text",
                        "default": "",
                        "placeholder": "e.g. grok-4.6",
                    },
                    {
                        "key": "api_key",
                        "label": "API Key",
                        "type": "password",
                        "default": "",
                        "placeholder": "xAI API Key",
                    },
                    {
                        "key": "base_url",
                        "label": "Base URL (optional)",
                        "type": "text",
                        "default": "",
                        "placeholder": "Default: https://api.x.ai/v1",
                    },
                ]
            },
        },
]
