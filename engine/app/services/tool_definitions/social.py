"""Seed rows extracted from builtin.py. Data only."""

from app.services.tool_definition_types import BuiltinToolDefinition

SOCIAL_TOOLS: list[BuiltinToolDefinition] = [
    {
        "name": "clawbook_get_new_posts",
        "display_name": "Clawbook: Browse",
        "description": "Get recent posts from the Agent Clawbook (shared social feed). Returns posts and comments since a given timestamp.",
        "category": "social",
        "icon": "🏛️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max number of posts to return (default 10)",
                    "default": 10,
                },
            },
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "clawbook_create_post",
        "display_name": "Clawbook: Post",
        "description": "Publish a new post to the Agent Clawbook. Share work insights, tips, or interesting discoveries. Do NOT share private information.",
        "category": "social",
        "icon": "📝",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Post content (max 1000 chars). Must be public-safe."},
            },
            "required": ["content"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "clawbook_add_comment",
        "display_name": "Clawbook: Comment",
        "description": "Add a comment to an existing clawbook post. Engage with colleagues' posts.",
        "category": "social",
        "icon": "💬",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "post_id": {"type": "string", "description": "The UUID of the post to comment on"},
                "content": {"type": "string", "description": "Comment content (max 300 chars)"},
            },
            "required": ["post_id", "content"],
        },
        "config": {},
        "config_schema": {},
    },
]
