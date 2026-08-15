"""OpenAI function-calling catalog slice. Data only."""

PAGES_AGENT_TOOLS: list[object] = [
        {
            "type": "function",
            "function": {
                "name": "publish_page",
                "description": "Publish an HTML file from workspace as a public page. Returns a public URL that anyone can access without login. Only .html/.htm files can be published.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path in workspace, e.g. 'workspace/output.html'",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_published_pages",
                "description": "List all pages published by this agent, showing their public URLs and view counts.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
]
