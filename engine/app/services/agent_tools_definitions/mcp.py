"""OpenAI function-calling catalog slice. Data only."""

MCP_AGENT_TOOLS: list[object] = [
        {
            "type": "function",
            "function": {
                "name": "discover_resources",
                "description": "Search public MCP registries (Smithery) for tools and capabilities that can extend your abilities. Use this when you encounter a task you cannot handle with your current tools.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Semantic description of the capability needed, e.g. 'send email', 'query SQL database', 'generate images'",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Max results to return (default 5, max 10)",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
]
