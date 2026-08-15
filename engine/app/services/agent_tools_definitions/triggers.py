"""OpenAI function-calling catalog slice. Data only."""

TRIGGER_AGENT_TOOLS: list[object] = [
        {
            "type": "function",
            "function": {
                "name": "set_trigger",
                "description": "Set a new trigger to wake yourself up at a specific time or condition. Use this to schedule future actions, monitor changes, or wait for messages. The trigger will fire and invoke you with the reason text as context. Every trigger is attached to a focus item; if focus_ref is omitted, the system will automatically create a focus item from the reason and attach the trigger to it. Trigger types: 'cron' (recurring schedule), 'once' (fire once at a time), 'interval' (every N minutes), 'poll' (HTTP monitoring), 'on_message' (when another agent or a human user replies - use from_agent_name for agents, or from_user_name for human users on Feishu/Slack/Discord), 'webhook' (receive external HTTP POST - system generates a unique URL, give it to the user so they can configure it in external services like GitHub, Grafana, etc.).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Unique name for this trigger, e.g. 'daily_briefing' or 'wait_morty_reply'",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["cron", "once", "interval", "poll", "on_message", "webhook"],
                            "description": "Trigger type",
                        },
                        "config": {
                            "type": "object",
                            "description": 'Type-specific config. cron: {"expr": "0 9 * * *"}. once: {"at": "2026-03-10T09:00:00+08:00"}. interval: {"minutes": 30}. poll: {"url": "...", "json_path": "$.status", "fire_on": "change", "interval_min": 5}. on_message: {"from_agent_name": "Morty"} or {"from_user_name": "张三"} (for human users on Feishu/Slack/Discord). webhook: {"secret": "optional_hmac_secret"} (system auto-generates the URL)',
                        },
                        "reason": {
                            "type": "string",
                            "description": "What you should do when this trigger fires. This will be shown to you as context when you wake up.",
                        },
                        "focus_ref": {
                            "type": "string",
                            "description": "Optional: identifier of the structured Focus item that this trigger relates to. If omitted, a Focus item is created automatically from the trigger reason.",
                        },
                    },
                    "required": ["name", "type", "config", "reason"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_trigger",
                "description": "Update an existing trigger's configuration or reason. Use this to adjust timing, change parameters, etc. For example, change interval from 5 minutes to 30 minutes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the trigger to update",
                        },
                        "config": {
                            "type": "object",
                            "description": "New config (replaces existing config)",
                        },
                        "reason": {
                            "type": "string",
                            "description": "New reason text",
                        },
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "cancel_trigger",
                "description": "Cancel (disable) a trigger by name. Use this when a task is completed and the trigger is no longer needed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the trigger to cancel",
                        },
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_triggers",
                "description": "List all your active triggers. Shows name, type, config, reason, fire count, and status.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
]
