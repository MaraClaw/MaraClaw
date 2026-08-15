"""OpenAI function-calling catalog slice. Data only."""

SKILL_AGENT_TOOLS: list[object] = [
        {
            "type": "function",
            "function": {
                "name": "search_clawhub",
                "description": "Search the ClawHub skill registry for skills matching a query. Returns a list of available skills with name, description, and last updated date. Use this to help users find skills to install.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query, e.g. 'research', 'code review', 'market analysis'",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "install_skill",
                "description": "Install a skill into this agent's workspace. Accepts either a ClawHub skill slug (e.g. 'market-research') or a GitHub URL (e.g. 'https://github.com/user/repo'). The skill files will be downloaded and saved to skills/<name>/ in your workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": "ClawHub skill slug (e.g. 'market-research') or GitHub URL (e.g. 'https://github.com/user/repo')",
                        },
                    },
                    "required": ["source"],
                },
            },
        },
]
