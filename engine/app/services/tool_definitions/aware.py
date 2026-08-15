"""Seed rows extracted from builtin.py. Data only."""


from typing import Any

AWARE_TOOLS: list[dict[str, Any]] = [
        {
            "name": "set_trigger",
            "display_name": "Set Trigger",
            "description": "Set a new trigger to wake yourself up at a specific time or condition. Every trigger is attached to a focus item; if focus_ref is omitted, the system creates a focus item from the reason. Trigger types: 'cron' (recurring schedule), 'once' (fire once at a time), 'interval' (every N minutes), 'poll' (HTTP monitoring), 'on_message' (when another agent or human user replies).",
            "category": "aware",
            "icon": "⚡",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Unique name for this trigger"},
                    "type": {
                        "type": "string",
                        "enum": ["cron", "once", "interval", "poll", "on_message"],
                        "description": "Trigger type",
                    },
                    "config": {
                        "type": "object",
                        "description": 'Type-specific config. cron: {"expr": "0 9 * * *"}. once: {"at": "2026-03-10T09:00:00+08:00"}. interval: {"minutes": 30}. poll: {"url": "...", "json_path": "$.status"}. on_message: {"from_agent_name": "Morty"} or {"from_user_name": "张三"}',
                    },
                    "reason": {"type": "string", "description": "What to do when this trigger fires"},
                    "focus_ref": {
                        "type": "string",
                        "description": "Optional: which focus item this relates to. If omitted, one is created automatically.",
                    },
                },
                "required": ["name", "type", "config", "reason"],
            },
            "config": {},
            "config_schema": {},
        },
        {
            "name": "update_trigger",
            "display_name": "Update Trigger",
            "description": "Update an existing trigger's configuration or reason.",
            "category": "aware",
            "icon": "🔄",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the trigger to update"},
                    "config": {"type": "object", "description": "New config (replaces existing)"},
                    "reason": {"type": "string", "description": "New reason text"},
                },
                "required": ["name"],
            },
            "config": {},
            "config_schema": {},
        },
        {
            "name": "cancel_trigger",
            "display_name": "Cancel Trigger",
            "description": "Cancel (disable) a trigger by name. Use when a task is completed.",
            "category": "aware",
            "icon": "⏹️",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the trigger to cancel"},
                },
                "required": ["name"],
            },
            "config": {},
            "config_schema": {},
        },
        {
            "name": "list_triggers",
            "display_name": "List Triggers",
            "description": "List all your active triggers with name, type, config, reason, fire count, and status.",
            "category": "aware",
            "icon": "📋",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {},
            },
            "config": {},
            "config_schema": {},
        },
]
