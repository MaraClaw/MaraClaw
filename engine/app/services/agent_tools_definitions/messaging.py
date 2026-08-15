"""OpenAI function-calling catalog slice. Data only."""

MESSAGING_AGENT_TOOLS: list[object] = [
        {
            "type": "function",
            "function": {
                "name": "send_channel_file",
                "description": "Send a file to a specific person or back to the current conversation. If member_name is provided, the system resolves the recipient across all connected channels (Feishu, Slack, etc.) and delivers the file via the appropriate channel. If member_name is omitted, the file is sent back through the current conversation channel.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Workspace-relative path to the file, e.g. workspace/report.md",
                        },
                        "member_name": {
                            "type": "string",
                            "description": "Name of the person to send the file to. If provided, the system looks up this person across all configured channels and delivers via the appropriate one.",
                        },
                        "message": {
                            "type": "string",
                            "description": "Optional message to accompany the file",
                        },
                    },
                    "required": ["file_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "send_feishu_message",
                "description": (
                    "Send a Feishu IM message to a colleague. "
                    + "You can provide either the colleague's name "
                    + "or their Feishu user_id directly. "
                    + "To contact digital employees use send_message_to_agent instead."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "member_name": {
                            "type": "string",
                            "description": "Recipient's name, e.g. '覃睿'. Will be looked up automatically.",
                        },
                        "user_id": {
                            "type": "string",
                            "description": "Recipient's Feishu user_id (preferred, tenant-stable). Get from feishu_user_search.",
                        },
                        "message": {
                            "type": "string",
                            "description": "Message content to send",
                        },
                    },
                    "required": ["message"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "send_channel_message",
                "description": (
                    "Send a message to a colleague via their configured external channel "
                    + "(Feishu, DingTalk, WeCom, Slack, MS Teams, Google Chat, WeChat). "
                    + "Automatically detects the recipient's channel based on their org relationship. "
                    + "Use this only for channel users. "
                    + "For relationships labeled Platform User / 平台用户, use send_platform_message instead."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "member_name": {
                            "type": "string",
                            "description": "Recipient's name as shown in relationships, e.g. '张三'. Must be a person in your relationship network.",
                        },
                        "message": {
                            "type": "string",
                            "description": "Message content to send",
                        },
                        "channel": {
                            "type": "string",
                            "description": (
                                "Optional: Specific channel to use when the recipient exists on multiple channels. "
                                + "Accepted values: feishu, dingtalk, wecom, slack, teams, microsoft_teams, "
                                + "google_chat, gchat, wechat."
                            ),
                            "enum": [
                                "feishu",
                                "dingtalk",
                                "wecom",
                                "slack",
                                "teams",
                                "microsoft_teams",
                                "google_chat",
                                "gchat",
                                "wechat",
                            ],
                        },
                    },
                    "required": ["member_name", "message"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "send_platform_message",
                "description": "Send a message to a user on the MaraClaw first-party platform (web or app). The message will appear in their platform chat history and be pushed in real-time if they are online. Use this to proactively notify platform users.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "username": {
                            "type": "string",
                            "description": "Username or display name of the recipient (must be a registered platform user)",
                        },
                        "message": {
                            "type": "string",
                            "description": "Message content to send",
                        },
                    },
                    "required": ["username", "message"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "send_message_to_agent",
                "description": "Send a message to a digital employee colleague. The recipient is another AI agent, not a human. Refer to the 'Relationships' section in your system prompt for available digital employees.\n\nDECISION GUIDE for msg_type:\nAsk yourself: does the target agent need to DO WORK (analyze, research, summarize, write, compare, plan, etc.) and RETURN RESULTS to you or the user?\n\n- If YES, the target needs to do work → use task_delegate. Examples: 'summarize X', 'analyze Y', 'check Z', 'prepare a report', 'review and give feedback', 'find out X', 'confirm with X and report back'. The target works asynchronously and you will be woken when they finish.\n\n- If the target just needs to KNOW something → use notify. Examples: 'meeting cancelled', 'I updated the doc', 'heads up about X', 'FYI'. No reply expected.\n\n- If you need a quick factual answer right now → use consult. Examples: 'what is X?', 'do you know Y?'. Synchronous, blocks until reply.\n\nWhen in doubt between notify and task_delegate, prefer task_delegate - it is safer because it guarantees the user gets a result.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_name": {
                            "type": "string",
                            "description": "Target digital employee's name",
                        },
                        "message": {
                            "type": "string",
                            "description": "Message content to send",
                        },
                        "msg_type": {
                            "type": "string",
                            "enum": ["notify", "consult", "task_delegate"],
                            "description": "Decision guide: (1) Will the target need to DO WORK and return results? → task_delegate. (2) Is this just a one-way FYI? → notify. (3) Quick factual question needing immediate answer? → consult. When unsure, prefer task_delegate.",
                        },
                    },
                    "required": ["agent_name", "message", "msg_type"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "send_file_to_agent",
                "description": "Send a workspace file to another digital employee. The file is copied into the target agent's workspace/inbox/files/ directory and a delivery note is created in their inbox.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_name": {
                            "type": "string",
                            "description": "Target digital employee's name",
                        },
                        "file_path": {
                            "type": "string",
                            "description": "Workspace-relative path of the source file, e.g. workspace/report.md",
                        },
                        "message": {
                            "type": "string",
                            "description": "Optional delivery note for the target digital employee",
                        },
                    },
                    "required": ["agent_name", "file_path"],
                },
            },
        },
]
