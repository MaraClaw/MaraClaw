"""Seed rows extracted from builtin.py. Data only."""


from typing import Any

COMMUNICATION_TOOLS: list[dict[str, Any]] = [
        {
            "name": "send_channel_file",
            "display_name": "Send File",
            "description": "Send a file to a specific person or back to the current conversation. If member_name is provided, the system resolves the recipient across all connected channels (Feishu, Slack, etc.) and delivers the file via the appropriate channel.",
            "category": "communication",
            "icon": "📎",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Workspace-relative path to the file"},
                    "member_name": {
                        "type": "string",
                        "description": "Name of the person to send the file to. The system looks up this person across all configured channels and delivers via the appropriate one.",
                    },
                    "message": {"type": "string", "description": "Optional message to accompany the file"},
                },
                "required": ["file_path"],
            },
            "config": {},
            "config_schema": {},
        },
        {
            "name": "send_platform_message",
            "display_name": "Platform Message",
            "description": "Send a proactive message to a user on the MaraClaw first-party platform (web or app). The message appears in their platform chat history and is pushed in real-time if they are online.",
            "category": "communication",
            "icon": "🌐",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "username": {"type": "string", "description": "Recipient username or display name"},
                    "message": {"type": "string", "description": "Message content"},
                },
                "required": ["username", "message"],
            },
            "config": {},
            "config_schema": {},
        },
        {
            "name": "send_message_to_agent",
            "display_name": "Agent Message",
            "description": "Send a message to a digital employee colleague. Decision guide: target needs to DO WORK and return results? → task_delegate. Just FYI? → notify. Quick factual question? → consult. When unsure, prefer task_delegate.",
            "category": "communication",
            "icon": "🤖",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Target agent name"},
                    "message": {"type": "string", "description": "Message content"},
                    "msg_type": {
                        "type": "string",
                        "enum": ["notify", "consult", "task_delegate"],
                        "description": "(1) Target needs to DO WORK and return results? → task_delegate. (2) Just FYI? → notify. (3) Quick factual question? → consult. When unsure, prefer task_delegate.",
                    },
                },
                "required": ["agent_name", "message", "msg_type"],
            },
            "config": {},
            "config_schema": {},
        },
        {
            "name": "send_file_to_agent",
            "display_name": "Agent File Transfer",
            "description": "Send a workspace file to another digital employee. The file is copied to the target agent's workspace/inbox/files/ and an inbox note is created.",
            "category": "communication",
            "icon": "📤",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Target agent name"},
                    "file_path": {"type": "string", "description": "Workspace-relative source file path"},
                    "message": {"type": "string", "description": "Optional delivery note"},
                },
                "required": ["agent_name", "file_path"],
            },
            "config": {},
            "config_schema": {},
        },
]
