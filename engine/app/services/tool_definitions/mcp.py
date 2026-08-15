"""Seed rows extracted from builtin.py. Data only."""


from typing import Any

MCP_TOOLS: list[dict[str, Any]] = [
        {
            "name": "discover_resources",
            "display_name": "Resource Discovery",
            "description": "Search public MCP registries (Smithery + ModelScope) for tools and capabilities that can extend your abilities. Use this when you encounter a task you cannot handle with your current tools.",
            "category": "discovery",
            "icon": "🔎",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Semantic description of the capability needed, e.g. 'send email', 'query SQL database', 'generate images'",
                    },
                    "max_results": {"type": "integer", "description": "Max results to return (default 5, max 10)"},
                },
                "required": ["query"],
            },
            "config": {},
            "config_schema": {
                "fields": [
                    {
                        "key": "smithery_api_key",
                        "label": "Smithery API Key",
                        "type": "password",
                        "default": "",
                        "placeholder": "Get your key at smithery.ai/account/api-keys",
                    },
                    {
                        "key": "modelscope_api_token",
                        "label": "ModelScope API Token",
                        "type": "password",
                        "default": "",
                        "placeholder": "Get your token at modelscope.cn → Home → Access Tokens",
                    },
                ]
            },
        },
        {
            "name": "import_mcp_server",
            "display_name": "Import MCP Server",
            "description": "Import an MCP server from Smithery registry into the platform. The server's tools become available for use. Use discover_resources first to find the server ID.",
            "category": "discovery",
            "icon": "📥",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "server_id": {
                        "type": "string",
                        "description": "Smithery server ID, e.g. '@anthropic/brave-search' or '@anthropic/fetch'",
                    },
                    "config": {
                        "type": "object",
                        "description": "Optional server configuration (e.g. API keys required by the server)",
                    },
                },
                "required": ["server_id"],
            },
            "config": {},
            "config_schema": {
                "fields": [
                    {
                        "key": "smithery_api_key",
                        "label": "Smithery API Key",
                        "type": "password",
                        "default": "",
                        "placeholder": "Get your key at smithery.ai/account/api-keys",
                    },
                    {
                        "key": "modelscope_api_token",
                        "label": "ModelScope API Token",
                        "type": "password",
                        "default": "",
                        "placeholder": "Get your token at modelscope.cn → Home → Access Tokens",
                    },
                ]
            },
        },
]
