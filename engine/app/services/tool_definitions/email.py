"""Seed rows extracted from builtin.py. Data only."""


from typing import Any

EMAIL_TOOLS: list[dict[str, Any]] = [
        {
            "name": "send_email",
            "display_name": "Send Email",
            "description": "Send an email to one or more recipients. Supports subject, body text, CC, and file attachments from workspace.",
            "category": "email",
            "icon": "📧",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address(es), comma-separated for multiple"},
                    "subject": {"type": "string", "description": "Email subject line"},
                    "body": {"type": "string", "description": "Email body text"},
                    "cc": {"type": "string", "description": "CC recipients, comma-separated (optional)"},
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of workspace-relative file paths to attach (optional). E.g. ['workspace/filename.ext']. Always specify this parameter if the user uploads a file or mentions sending/attaching a file.",
                    },
                },
                "required": ["to", "subject", "body"],
            },
            "config": {},
            "config_schema": {
                "fields": [
                    {
                        "key": "email_provider",
                        "label": "Email Provider",
                        "type": "select",
                        "options": [
                            {
                                "value": "gmail",
                                "label": "Gmail",
                                "help_text": "Google Account → Security → App passwords → Generate app password",
                                "help_url": "https://support.google.com/accounts/answer/185833",
                            },
                            {
                                "value": "outlook",
                                "label": "Outlook / Microsoft 365",
                                "help_text": "Microsoft Account → Security → App passwords",
                                "help_url": "https://support.microsoft.com/en-us/account-billing/manage-app-passwords-for-two-step-verification-d6dc8c6d-4bf7-4851-ad95-6d07799387e9",
                            },
                            {
                                "value": "qq",
                                "label": "QQ Mail",
                                "help_text": "Settings → Account → POP3/IMAP/SMTP → Enable IMAP → Generate authorization code",
                                "help_url": "https://service.mail.qq.com/detail/0/310",
                            },
                            {
                                "value": "163",
                                "label": "163 Mail",
                                "help_text": "Settings → POP3/SMTP/IMAP → Enable IMAP → Set authorization code",
                                "help_url": "https://help.mail.163.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac2",
                            },
                            {
                                "value": "qq_enterprise",
                                "label": "Tencent Enterprise Mail",
                                "help_text": "Enterprise Mail → Settings → Client-specific password → Generate new password",
                                "help_url": "https://open.work.weixin.qq.com/help2/pc/18624",
                            },
                            {
                                "value": "aliyun",
                                "label": "Alibaba Enterprise Mail",
                                "help_text": "Use your email password directly",
                                "help_url": "",
                            },
                            {
                                "value": "custom",
                                "label": "Custom",
                                "help_text": "Use the authorization code or app password from your email provider",
                                "help_url": "",
                            },
                        ],
                        "default": "gmail",
                    },
                    {
                        "key": "email_address",
                        "label": "Email Address",
                        "type": "text",
                        "placeholder": "your@email.com",
                    },
                    {
                        "key": "auth_code",
                        "label": "Authorization Code",
                        "type": "password",
                        "placeholder": "Authorization code (not your login password)",
                    },
                    {
                        "key": "imap_host",
                        "label": "IMAP Host",
                        "type": "text",
                        "placeholder": "imap.example.com",
                        "depends_on": {"email_provider": ["custom"]},
                    },
                    {
                        "key": "imap_port",
                        "label": "IMAP Port",
                        "type": "number",
                        "default": 993,
                        "depends_on": {"email_provider": ["custom"]},
                    },
                    {
                        "key": "smtp_host",
                        "label": "SMTP Host",
                        "type": "text",
                        "placeholder": "smtp.example.com",
                        "depends_on": {"email_provider": ["custom"]},
                    },
                    {
                        "key": "smtp_port",
                        "label": "SMTP Port",
                        "type": "number",
                        "default": 465,
                        "depends_on": {"email_provider": ["custom"]},
                    },
                ]
            },
        },
        {
            "name": "read_emails",
            "display_name": "Read Emails",
            "description": "Read emails from your inbox. Can limit the number returned and search by criteria (e.g. FROM, SUBJECT, SINCE date).",
            "category": "email",
            "icon": "📬",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max number of emails to return (default 10, max 30)",
                        "default": 10,
                    },
                    "search": {
                        "type": "string",
                        "description": "IMAP search criteria, e.g. 'FROM \"john@example.com\"', 'SUBJECT \"meeting\"', 'SINCE 01-Mar-2026'. Default: all emails.",
                    },
                    "folder": {"type": "string", "description": "Mailbox folder (default INBOX)", "default": "INBOX"},
                },
            },
            "config": {},
            "config_schema": {},
        },
        {
            "name": "reply_email",
            "display_name": "Reply Email",
            "description": "Reply to an email by its Message-ID. Maintains the email thread with proper In-Reply-To headers.",
            "category": "email",
            "icon": "↩️",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Message-ID of the email to reply to (from read_emails output)",
                    },
                    "body": {"type": "string", "description": "Reply body text"},
                },
                "required": ["message_id", "body"],
            },
            "config": {},
            "config_schema": {},
        },
]
