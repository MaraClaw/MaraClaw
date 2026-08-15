"""OpenAI function-calling catalog slice. Data only."""

CODE_MEDIA_AGENT_TOOLS: list[object] = [
        {
            "type": "function",
            "function": {
                "name": "execute_code",
                "description": "Execute code (Python, Bash, or Node.js) in a local sandboxed subprocess within the agent's root directory. Useful for data processing, calculations, file transformations, and automation scripts. Code runs with the agent root as the working directory, so you can access skills/, workspace/, memory/ etc. directly. Security restrictions apply: no system-level operations, 30-second default timeout.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "language": {
                            "type": "string",
                            "enum": ["python", "bash", "node"],
                            "description": "Programming language to execute",
                        },
                        "code": {
                            "type": "string",
                            "description": "Code to execute. If a Python import fails due to a missing package, install it first via execute_code with language='bash' and code='pip install <package>'. Working directory is the agent root (skills/, workspace/, memory/ are accessible).",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Max execution time in seconds (default 60, max 3600)",
                        },
                    },
                    "required": ["language", "code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "execute_code_e2b",
                "description": "Execute code (Python, Bash, or Node.js) in a secure E2B cloud sandbox. The sandbox has full network access and is fully isolated from the server. Use this when local execution is insufficient or when network access is required inside the code.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "language": {
                            "type": "string",
                            "enum": ["python", "bash", "node"],
                            "description": "Programming language to execute",
                        },
                        "code": {
                            "type": "string",
                            "description": "Code to execute in the E2B cloud sandbox.",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Max execution time in seconds (default 30, max 60)",
                        },
                    },
                    "required": ["language", "code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "upload_image",
                "description": "Upload an image file from your workspace (or from a public URL) to a cloud CDN and get a permanent public URL. Use this when you need to share images externally, embed them in messages/reports, or make workspace images accessible via URL. Supports common formats: PNG, JPG, GIF, WebP, SVG.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Workspace-relative path to the image file, e.g. workspace/chart.png or workspace/knowledge_base/diagram.jpg",
                        },
                        "url": {
                            "type": "string",
                            "description": "Alternative: a public URL of an image to upload (e.g. https://example.com/photo.jpg). Use this instead of file_path when the image is not in your workspace.",
                        },
                        "file_name": {
                            "type": "string",
                            "description": "Optional custom filename for the uploaded image. If omitted, the original filename is used.",
                        },
                        "folder": {
                            "type": "string",
                            "description": "Optional CDN folder path, e.g. /agents/reports. Defaults to /maraclaw.",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "generate_image_siliconflow",
                "description": "Generate an image via SiliconFlow (FLUX). Save to workspace. Fast and China-friendly.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Detailed image description in English.",
                        },
                        "size": {
                            "type": "string",
                            "description": "Image size. Default: 1024x1024. Options: 1024x1024, 1024x768, 768x1024",
                        },
                        "save_path": {
                            "type": "string",
                            "description": "Workspace path to save the image (e.g. workspace/images/sunset.png).",
                        },
                    },
                    "required": ["prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "generate_image_openai",
                "description": "Generate an image via OpenAI. Save to workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Detailed image description in English.",
                        },
                        "size": {
                            "type": "string",
                            "description": "Image size. Default: 1024x1024.",
                        },
                        "save_path": {
                            "type": "string",
                            "description": "Workspace path to save the image.",
                        },
                    },
                    "required": ["prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "generate_image_google",
                "description": "Generate an image via Google Gemini Image (Nano Banana) or Vertex AI. Save to workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Detailed image description in English.",
                        },
                        "size": {
                            "type": "string",
                            "description": "Image size. Default: 1024x1024.",
                        },
                        "save_path": {
                            "type": "string",
                            "description": "Workspace path to save the image.",
                        },
                    },
                    "required": ["prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "generate_image_grok",
                "description": "Generate an image via xAI Grok Imagine, the default image provider. Prefer this over other generate_image_* tools. Billed per image on the configured xAI key. Save to workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Detailed image description in English.",
                        },
                        "size": {
                            "type": "string",
                            "description": "Image size. Default: 1024x1024. WxH is mapped to a Grok Imagine aspect ratio.",
                        },
                        "save_path": {
                            "type": "string",
                            "description": "Workspace path to save the image.",
                        },
                    },
                    "required": ["prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "generate_image_custom",
                "description": "Generate an image via the company-configured custom image API. Save to workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Detailed image description in English.",
                        },
                        "size": {
                            "type": "string",
                            "description": "Image size. Default: 1024x1024.",
                        },
                        "save_path": {
                            "type": "string",
                            "description": "Workspace path to save the image.",
                        },
                    },
                    "required": ["prompt"],
                },
            },
        },
]
