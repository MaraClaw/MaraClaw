"""Seed rows extracted from builtin.py. Data only."""


from typing import Any

MEDIA_TOOLS: list[dict[str, Any]] = [
        {
            "name": "generate_image_siliconflow",
            "display_name": "Generate Image (SiliconFlow)",
            "description": "Generate an image via SiliconFlow FLUX models. China-friendly and fast.",
            "category": "media",
            "icon": "🎨",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed image description."},
                    "size": {"type": "string", "description": "Image size (e.g. 1024x1024, 1024x768). Default 1024x1024."},
                    "save_path": {"type": "string", "description": "Save path in workspace. Default: auto."},
                },
                "required": ["prompt"],
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
                        "placeholder": "e.g. black-forest-labs/FLUX.1-schnell",
                    },
                    {
                        "key": "api_key",
                        "label": "API Key",
                        "type": "password",
                        "default": "",
                        "placeholder": "SiliconFlow API Key",
                    },
                    {
                        "key": "base_url",
                        "label": "Base URL (optional)",
                        "type": "text",
                        "default": "",
                        "placeholder": "Default: https://api.siliconflow.cn/v1",
                    },
                ]
            },
        },
        {
            "name": "generate_image_openai",
            "display_name": "Generate Image (OpenAI)",
            "description": "Generate an image via OpenAI DALL-E models.",
            "category": "media",
            "icon": "🎨",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed image description."},
                    "size": {"type": "string", "description": "Image size (e.g. 1024x1024). Default 1024x1024."},
                    "save_path": {"type": "string", "description": "Save path in workspace. Default: auto."},
                },
                "required": ["prompt"],
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
                        "placeholder": "e.g. dall-e-3 or dall-e-2",
                    },
                    {
                        "key": "api_key",
                        "label": "API Key",
                        "type": "password",
                        "default": "",
                        "placeholder": "OpenAI API Key",
                    },
                    {
                        "key": "base_url",
                        "label": "Base URL (optional)",
                        "type": "text",
                        "default": "",
                        "placeholder": "Default: https://api.openai.com/v1",
                    },
                ]
            },
        },
        {
            "name": "generate_image_google",
            "display_name": "Generate Image (Google/Vertex)",
            "description": "Generate an image via Google Gemini Image (Nano Banana) or Vertex AI.",
            "category": "media",
            "icon": "🎨",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed image description."},
                    "size": {"type": "string", "description": "Image size (e.g. 1024x1024). Default 1024x1024."},
                    "save_path": {"type": "string", "description": "Save path in workspace. Default: auto."},
                },
                "required": ["prompt"],
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
                        "placeholder": "e.g. gemini-2.5-flash-image",
                    },
                    {
                        "key": "api_key",
                        "label": "API Key",
                        "type": "password",
                        "default": "",
                        "placeholder": "Google AI Studio or Vertex API Key",
                    },
                    {
                        "key": "base_url",
                        "label": "Base URL (optional)",
                        "type": "text",
                        "default": "",
                        "placeholder": "Can be Vertex API URL: https://aiplatform.googleapis.com/...",
                    },
                ]
            },
        },
        {
            "name": "generate_image_grok",
            "display_name": "Generate Image (Grok)",
            "description": "Generate an image via xAI Grok Imagine, the default image provider. Prefer this over other generate_image_* tools. Billed per image on the configured xAI key.",
            "category": "media",
            "icon": "🎨",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed image description."},
                    "size": {"type": "string", "description": "Image size (e.g. 1024x1024). Default 1024x1024. Mapped to Grok Imagine aspect ratio."},
                    "save_path": {"type": "string", "description": "Save path in workspace. Default: auto."},
                },
                "required": ["prompt"],
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
                        "placeholder": "e.g. grok-imagine-image-2.0",
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
        {
            "name": "generate_image_custom",
            "display_name": "Generate Image (Custom API)",
            "description": "Generate an image through a custom OpenAI-compatible or gateway API. Configure the request body template and response image path for providers such as TokenRouter or OpenRouter.",
            "category": "media",
            "icon": "🎨",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed image description."},
                    "size": {"type": "string", "description": "Image size (e.g. 1024x1024). Default 1024x1024."},
                    "save_path": {"type": "string", "description": "Save path in workspace. Default: auto."},
                },
                "required": ["prompt"],
            },
            "config": {
                "api_key": "",
                "base_url": "",
                "endpoint_path": "/chat/completions",
                "model": "",
                "request_body_template_json": '{\n  "model": "{model}",\n  "messages": [\n    {\n      "role": "user",\n      "content": "{prompt}"\n    }\n  ],\n  "modalities": ["image", "text"],\n  "stream": false\n}',
                "response_image_path": "choices.0.message.images.0.image_url.url",
                "extra_headers_json": "",
                "timeout_seconds": 120,
            },
            "config_schema": {
                "fields": [
                    {
                        "key": "api_key",
                        "label": "API Key",
                        "type": "password",
                        "default": "",
                        "placeholder": "API key for your image generation gateway",
                    },
                    {
                        "key": "model",
                        "label": "Model",
                        "type": "text",
                        "default": "",
                        "placeholder": "e.g. google/gemini-2.5-flash-image",
                    },
                    {
                        "key": "base_url",
                        "label": "Base URL",
                        "type": "text",
                        "default": "",
                        "placeholder": "e.g. https://api.tokenrouter.com/v1 or https://openrouter.ai/api/v1",
                    },
                    {
                        "key": "endpoint_path",
                        "label": "Endpoint Path",
                        "type": "text",
                        "default": "/chat/completions",
                        "placeholder": "/chat/completions",
                        "advanced": True,
                    },
                    {
                        "key": "request_body_template_json",
                        "label": "Request Body Template JSON",
                        "type": "textarea",
                        "default": '{\n  "model": "{model}",\n  "messages": [\n    {\n      "role": "user",\n      "content": "{prompt}"\n    }\n  ],\n  "modalities": ["image", "text"],\n  "stream": false\n}',
                        "placeholder": '{\n  "model": "{model}",\n  "messages": [{"role": "user", "content": "{prompt}"}],\n  "modalities": ["image", "text"],\n  "stream": false\n}',
                        "advanced": True,
                    },
                    {
                        "key": "response_image_path",
                        "label": "Response Image Path",
                        "type": "text",
                        "default": "choices.0.message.images.0.image_url.url",
                        "placeholder": "choices.0.message.images.0.image_url.url",
                        "advanced": True,
                    },
                    {
                        "key": "extra_headers_json",
                        "label": "Extra Headers JSON",
                        "type": "textarea",
                        "default": "",
                        "placeholder": '{\n  "HTTP-Referer": "https://your-app.example",\n  "X-Title": "MaraClaw"\n}',
                        "advanced": True,
                    },
                    {
                        "key": "timeout_seconds",
                        "label": "Timeout Seconds",
                        "type": "number",
                        "default": 120,
                        "min": 10,
                        "max": 600,
                        "advanced": True,
                    },
                ]
            },
        },
]
