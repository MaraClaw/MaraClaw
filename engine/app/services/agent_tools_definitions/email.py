"""OpenAI function-calling catalog slice. Data only."""

EMAIL_AGENT_TOOLS: list[object] = [
        {
            "type": "function",
            "function": {
                "name": "send_email",
                "description": "Send an email to one or more recipients. Supports subject, body text, CC, and file attachments from workspace. Requires email configuration in tool settings.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {
                            "type": "string",
                            "description": "Recipient email address(es), comma-separated for multiple",
                        },
                        "subject": {
                            "type": "string",
                            "description": "Email subject line",
                        },
                        "body": {
                            "type": "string",
                            "description": "Email body text",
                        },
                        "cc": {
                            "type": "string",
                            "description": "CC recipients, comma-separated (optional)",
                        },
                        "attachments": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of workspace-relative file paths to attach (optional)",
                        },
                    },
                    "required": ["to", "subject", "body"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_emails",
                "description": "Read emails from your inbox. Can limit the number returned and search by criteria (e.g. FROM, SUBJECT, SINCE date). Requires email configuration in tool settings.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Max number of emails to return (default 10, max 30)",
                        },
                        "search": {
                            "type": "string",
                            "description": "IMAP search criteria, e.g. 'FROM \"john@example.com\"', 'SUBJECT \"meeting\"', 'SINCE 01-Mar-2026'. Default: all emails.",
                        },
                        "folder": {
                            "type": "string",
                            "description": "Mailbox folder, default INBOX",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "reply_email",
                "description": "Reply to an email by its Message-ID. Maintains the email thread with proper In-Reply-To headers. Requires email configuration in tool settings.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message_id": {
                            "type": "string",
                            "description": "Message-ID of the email to reply to (from read_emails output)",
                        },
                        "body": {
                            "type": "string",
                            "description": "Reply body text",
                        },
                    },
                    "required": ["message_id", "body"],
                },
            },
        },
]
