"""OpenAI function-calling catalog slice. Data only."""

AGENTBAY_AGENT_TOOLS: list[object] = [
        {
            "type": "function",
            "function": {
                "name": "agentbay_browser_navigate",
                "description": "使用 AgentBay 浏览器环境访问指定 URL。访问后会自动截图以便你观察当前页面状态。Tip: after navigating, use browser_observe to identify elements, then browser_type/browser_click to interact. IMPORTANT: Do NOT call navigate again after clicking or typing - that will refresh the page and lose all your progress. Use agentbay_browser_screenshot instead.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "要访问的网址，如 https://example.com"},
                        "wait_for": {"type": "string", "description": "等待特定元素出现的选择器（可选）"},
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agentbay_browser_screenshot",
                "description": "Take a screenshot of the CURRENT browser page without navigating anywhere. Use this after clicking, typing, or submitting a form to verify the result - it preserves the current page state. Never call browser_navigate just to take a screenshot.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agentbay_browser_click",
                "description": "在 AgentBay 浏览器中点击指定元素。selector 可以是 CSS 选择器（如 #btn）或自然语言描述（如 'the Send button' 或 '发送验证码按钮'）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "CSS selector (e.g. #button) or natural language description of the element (e.g. 'the blue Submit button')",
                        },
                    },
                    "required": ["selector"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agentbay_browser_type",
                "description": "在 AgentBay 浏览器的输入框中输入文本。selector 可以是 CSS 选择器或自然语言描述（如 'phone number input' 或 '手机号输入框'）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "CSS selector or natural language description of the input field (e.g. 'the phone number input' or 'input[type=tel]')",
                        },
                        "text": {"type": "string", "description": "要输入的文本"},
                    },
                    "required": ["selector", "text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agentbay_browser_login",
                "description": "Use AgentBay's AI-driven login skill to automate complex login flows (CAPTCHAs, OTP, multi-step auth). Requires a login_config JSON with AgentBay skill credentials. Navigate to the login page and execute the login skill.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "The login page URL to navigate to"},
                        "login_config": {
                            "type": "string",
                            "description": 'JSON string with login config, e.g. \'{"api_key": "xxx", "skill_id": "yyy"}\'',
                        },
                    },
                    "required": ["url", "login_config"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agentbay_code_execute",
                "description": "在 AgentBay 代码空间中执行代码。支持 Python、Bash、Node.js。需要先配置 AgentBay 通道。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "language": {
                            "type": "string",
                            "enum": ["python", "bash", "node"],
                            "description": "编程语言",
                        },
                        "code": {"type": "string", "description": "要执行的代码"},
                        "timeout": {"type": "integer", "description": "超时时间（秒，默认 30）", "default": 30},
                    },
                    "required": ["language", "code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agentbay_code_write_file",
                "description": "[ENV: Code Sandbox] Write a text file inside the AgentBay Code Sandbox.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "remote_path": {
                            "type": "string",
                            "description": "Absolute path inside the code sandbox, e.g. /home/wuying/main.py",
                        },
                        "content": {"type": "string", "description": "File content to write."},
                        "mode": {
                            "type": "string",
                            "enum": ["overwrite", "append"],
                            "description": "Write mode. Default: overwrite.",
                            "default": "overwrite",
                        },
                    },
                    "required": ["remote_path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agentbay_code_read_file",
                "description": "[ENV: Code Sandbox] Read a text file from the AgentBay Code Sandbox.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "remote_path": {
                            "type": "string",
                            "description": "Absolute path inside the code sandbox, e.g. /home/wuying/main.py",
                        },
                    },
                    "required": ["remote_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agentbay_code_edit_file",
                "description": "[ENV: Code Sandbox] Edit a text file inside the AgentBay Code Sandbox by replacing exact text.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "remote_path": {
                            "type": "string",
                            "description": "Absolute path inside the code sandbox, e.g. /home/wuying/main.py",
                        },
                        "edits": {
                            "type": "array",
                            "description": "List of exact text replacements.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "oldText": {"type": "string", "description": "Exact text to replace."},
                                    "newText": {"type": "string", "description": "Replacement text."},
                                },
                                "required": ["oldText", "newText"],
                            },
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Preview changes without applying them. Default: false.",
                            "default": False,
                        },
                    },
                    "required": ["remote_path", "edits"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agentbay_file_transfer",
                "description": (
                    "Transfer a file between any two endpoints: the agent workspace, "
                    + "the AgentBay browser environment, the cloud desktop (computer), or the code sandbox.\n\n"
                    + "VERIFIED PATH CONVENTIONS (all Linux environments run as user 'wuying', HOME=/home/wuying/):\n"
                    + "- code env:     use /home/wuying/<filename>  (working directory, e.g. /home/wuying/data.csv)\n"
                    + "- browser env:  use /home/wuying/下载/<filename>  (download folder, e.g. /home/wuying/下载/file.pdf)\n"
                    + "- computer env: use /home/wuying/桌面/<filename>  (Desktop, e.g. /home/wuying/桌面/report.xlsx)\n"
                    + "- workspace:    use relative path, e.g. 'workspace/data.csv'\n\n"
                    + "Transfer directions:\n"
                    + "- workspace -> env: upload a workspace file into a cloud environment\n"
                    + "- env -> workspace: download a file from a cloud environment into the workspace\n"
                    + "- env A -> env B:   transfer between environments (transparent backend temp, no workspace involvement)"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "from_type": {
                            "type": "string",
                            "enum": ["workspace", "browser", "computer", "code"],
                            "description": "Source endpoint: 'workspace' for agent workspace, or the AgentBay environment name.",
                        },
                        "from_path": {
                            "type": "string",
                            "description": (
                                "Source path. Relative if workspace (e.g. 'workspace/data.csv'). "
                                + "Absolute if env: code → /home/wuying/file, "
                                + "browser → /home/wuying/下载/file, computer → /home/wuying/桌面/file."
                            ),
                        },
                        "to_type": {
                            "type": "string",
                            "enum": ["workspace", "browser", "computer", "code"],
                            "description": "Destination endpoint: 'workspace' for agent workspace, or the AgentBay environment name.",
                        },
                        "to_path": {
                            "type": "string",
                            "description": (
                                "Destination path. Relative if workspace (e.g. 'workspace/output.csv'). "
                                + "Absolute if env: code → /home/wuying/file, "
                                + "browser → /home/wuying/下载/file, computer → /home/wuying/桌面/file."
                            ),
                        },
                    },
                    "required": ["from_type", "from_path", "to_type", "to_path"],
                },
            },
        },
]
