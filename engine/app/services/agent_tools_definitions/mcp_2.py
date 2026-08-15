"""OpenAI function-calling catalog slice. Data only."""

MCP_AGENT_TOOLS_2: list[object] = [
        {
            "type": "function",
            "function": {
                "name": "import_mcp_server",
                "description": "Import an MCP server from Smithery registry into the platform. The server's tools become available for use. Use discover_resources first to find the server ID. If previously imported tools stopped working (e.g. OAuth expired), set reauthorize=true to re-run the authorization flow.",
                "parameters": {
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
                        "reauthorize": {
                            "type": "boolean",
                            "description": "Set to true to force re-authorization of existing tools (e.g. when OAuth token has expired)",
                        },
                    },
                    "required": ["server_id"],
                },
            },
        },
]
