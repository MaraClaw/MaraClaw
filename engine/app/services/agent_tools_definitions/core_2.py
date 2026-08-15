"""OpenAI function-calling catalog slice. Data only."""

CORE_AGENT_TOOLS_2: list[object] = [
        {
            "type": "function",
            "function": {
                "name": "read_webpage",
                "description": "Fetch a public HTTP/HTTPS URL directly and extract readable webpage text. Use this when you already have a specific link and need the page content without relying on an external reader service. Private, local, and internal network URLs are blocked.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The full public HTTP/HTTPS URL of the web page to read, e.g. 'https://example.com/article'",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "Max characters to return (default 12000, max 50000)",
                        },
                        "include_links": {
                            "type": "boolean",
                            "description": "Include up to 30 extracted page links (default false)",
                        },
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_x",
                "description": "Search X (Twitter) posts, users, and threads via xAI X Search. Use for live public conversation, handles, hashtags, and recent posts. Billed as a grok-4.6 Responses call plus X Search. enable_image_understanding and enable_video_understanding add extra cost; leave them false unless needed.",
                "parameters": {
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
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_document",
                "description": "Read office document contents (PDF, Word, Excel, PPT, etc.) and extract text. Suitable for reading knowledge base documents.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Document file path, e.g.: workspace/knowledge_base/report.pdf, enterprise_info/knowledge_base/policy.docx",
                        }
                    },
                    "required": ["path"],
                },
            },
        },
]
