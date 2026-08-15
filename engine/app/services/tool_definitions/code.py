"""Seed rows extracted from builtin.py. Data only."""


from typing import Any

CODE_TOOLS: list[dict[str, Any]] = [
        {
            "name": "execute_code",
            "display_name": "Code Executor",
            "description": "Execute code (Python, Bash, Node.js) in a local sandboxed subprocess within the agent's workspace. Useful for data processing, calculations, file transformations, and automation.",
            "category": "code",
            "icon": "💻",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["python", "bash", "node"],
                        "description": "Programming language",
                    },
                    "code": {"type": "string", "description": "Code to execute"},
                    "timeout": {"type": "integer", "description": "Max execution time in seconds (default 30, max 60)"},
                },
                "required": ["language", "code"],
            },
            "config": {
                "sandbox_type": "subprocess",
                "cpu_limit": "0.5",
                "memory_limit": "256m",
                "allow_network": True,
                "default_timeout": 30,
                "max_timeout": 60,
            },
            "config_schema": {
                "fields": [
                    {
                        "key": "cpu_limit",
                        "label": "CPU Limit",
                        "type": "text",
                        "default": "0.5",
                        "placeholder": "e.g., 0.5, 1.0, 2.0",
                    },
                    {
                        "key": "memory_limit",
                        "label": "Memory Limit",
                        "type": "text",
                        "default": "256m",
                        "placeholder": "e.g., 256m, 512m, 1g",
                    },
                    {
                        "key": "allow_network",
                        "label": "Allow Network Access",
                        "type": "checkbox",
                        "default": True,
                        "read_only_for_roles": ["agent_admin", "member"],
                    },
                    {
                        "key": "default_timeout",
                        "label": "Default Timeout (seconds)",
                        "type": "number",
                        "default": 30,
                        "min": 5,
                        "max": 3600,
                    },
                    {
                        "key": "max_timeout",
                        "label": "Max Timeout (seconds)",
                        "type": "number",
                        "default": 60,
                        "min": 10,
                        "max": 3600,
                    },
                ]
            },
        },
        {
            "name": "execute_code_e2b",
            "display_name": "Code Executor (E2B Cloud)",
            "description": "Execute code (Python, Bash, Node.js) in a secure E2B cloud sandbox. Provides full network access and an isolated environment without consuming local resources. Requires an E2B API key.",
            "category": "code",
            "icon": "☁️",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["python", "bash", "node"],
                        "description": "Programming language",
                    },
                    "code": {"type": "string", "description": "Code to execute"},
                    "timeout": {"type": "integer", "description": "Max execution time in seconds (default 30, max 60)"},
                },
                "required": ["language", "code"],
            },
            "config": {
                "sandbox_type": "e2b",
                "api_key": "",
                "default_timeout": 30,
                "max_timeout": 60,
            },
            "config_schema": {
                "fields": [
                    {
                        "key": "api_key",
                        "label": "E2B API Key",
                        "type": "password",
                        "default": "",
                        "placeholder": "Get your API key at https://e2b.dev",
                        "required": True,
                    },
                    {
                        "key": "default_timeout",
                        "label": "Default Timeout (seconds)",
                        "type": "number",
                        "default": 30,
                        "min": 5,
                        "max": 3600,
                    },
                    {
                        "key": "max_timeout",
                        "label": "Max Timeout (seconds)",
                        "type": "number",
                        "default": 60,
                        "min": 10,
                        "max": 3600,
                    },
                ]
            },
        },
        {
            "name": "upload_image",
            "display_name": "Upload Image",
            "description": "Upload images from the workspace or a URL to ImageKit CDN and get a public URL. Useful for sharing images externally or embedding them in reports.",
            "category": "code",
            "icon": "🖼️",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Workspace-relative path to image file"},
                    "url": {"type": "string", "description": "Public URL of image to upload"},
                    "file_name": {"type": "string", "description": "Custom filename (optional)"},
                    "folder": {"type": "string", "description": "CDN folder path (default /maraclaw)"},
                },
            },
            "config": {"private_key": "", "url_endpoint": ""},
            "config_schema": {
                "fields": [
                    {
                        "key": "private_key",
                        "label": "ImageKit Private Key",
                        "type": "password",
                        "default": "",
                        "placeholder": "Your ImageKit private API key",
                    },
                    {
                        "key": "url_endpoint",
                        "label": "ImageKit URL Endpoint",
                        "type": "text",
                        "default": "",
                        "placeholder": "https://ik.imagekit.io/your_imagekit_id",
                    },
                ]
            },
        },
]
