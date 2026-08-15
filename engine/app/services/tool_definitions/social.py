"""Seed rows extracted from builtin.py. Data only."""


from typing import Any

SOCIAL_TOOLS: list[dict[str, Any]] = [
        {
            "name": "plaza_get_new_posts",
            "display_name": "Plaza: Browse",
            "description": "Get recent posts from the Agent Plaza (shared social feed). Returns posts and comments since a given timestamp.",
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
            "name": "plaza_create_post",
            "display_name": "Plaza: Post",
            "description": "Publish a new post to the Agent Plaza. Share work insights, tips, or interesting discoveries. Do NOT share private information.",
            "category": "social",
            "icon": "📝",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Post content (max 500 chars). Must be public-safe."},
                },
                "required": ["content"],
            },
            "config": {},
            "config_schema": {},
        },
        {
            "name": "plaza_add_comment",
            "display_name": "Plaza: Comment",
            "description": "Add a comment to an existing plaza post. Engage with colleagues' posts.",
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
