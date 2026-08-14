from app.services.llm.finish import FINISH_TOOL_DEFINITION, FINISH_TOOL_NAME

# allow: SIZE_OK - static OpenAI tool catalog extracted verbatim; pure data table.
# ─── Tool Definitions (OpenAI function-calling format) ──────────

AGENT_TOOLS: list[object] = [
    FINISH_TOOL_DEFINITION,
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and folders in a directory within my workspace. Use this before writing new workspace documents so you can inspect the current folder structure, reuse existing topical subfolders when appropriate, and avoid dumping files directly into the workspace root unless there is a clear reason. Can also list enterprise_info/ for shared company information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to list, defaults to root (empty string). e.g.: '', 'skills', 'workspace', 'enterprise_info', 'enterprise_info/knowledge_base'",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents from the workspace. Can read soul.md for personality, memory/memory.md for memory, skills/ for skill files, and enterprise_info/ for shared company info. Focus is not stored in files; use list_focus_items and upsert_focus_item for Focus. Use offset and limit for reading large files in chunks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path, e.g.: soul.md, memory/memory.md, skills/xxx.md, enterprise_info/company_profile.md",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Starting line number (0-indexed, default 0). Use with limit for pagination.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to read (default 2000). Use with offset for pagination.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_focus_items",
            "description": "List your structured Focus items. Focus is your current working state and is stored in the system database, not in focus.md.",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_completed": {
                        "type": "boolean",
                        "description": "Whether to include completed Focus items. Default true.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upsert_focus_item",
            "description": "Create or update one Focus item in structured storage. Use this whenever you start tracking an active task, reminder, delegated wait, or system concern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Stable short identifier, snake_case preferred. If omitted, the system derives one from description.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Short title (Focus名称). Use this for a quick summary of the focus. Keep it brief. New focus items should have both a title and a description.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Clear human-readable description of what is being tracked.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["normal", "system"],
                        "description": "Use normal for user/business work, system for platform-maintained focus such as heartbeat/OKR automation.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Optional origin label, e.g. user, trigger, a2a, okr.",
                    },
                },
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_focus_item",
            "description": "Mark a Focus item completed. Use this after the tracked task/reminder/wait has been handled.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Focus item identifier to complete.",
                    }
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or fully overwrite a file in the workspace. Use this when writing a new file or replacing the entire content. For targeted edits to an existing file (change one section without rewriting everything), prefer edit_file instead. Before creating a new document under workspace/, first inspect the relevant directories with list_files, prefer an existing topical subfolder (for example workspace/reports/, workspace/knowledge_base/, workspace/research/) over the workspace root, and create a new subfolder when the content belongs to a new category. Avoid placing standalone document files directly in workspace/ root unless the user explicitly wants that. Can update memory/memory.md, task_history.md, create documents in workspace/, create skills in skills/. Focus is managed with Focus tools, not files. enterprise_info/ is shared company context and is read-only for agents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path, e.g.: memory/memory.md, workspace/reports/report.md, workspace/knowledge_base/notes.md, skills/data_analysis.md. Prefer a meaningful subfolder instead of writing loose files into workspace/ root.",
                    },
                    "content": {
                        "type": "string",
                        "description": "File content to write",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file from the workspace. Cannot delete soul.md, tasks.json, or shared enterprise_info/ files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path to delete",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "Move or rename a file or folder within the workspace. Use this instead of execute_code for reorganizing workspace files, moving generated documents into subfolders, or renaming files. Cannot move protected files or shared enterprise_info/ files. If destination_path is an existing folder or ends with '/', the original filename is preserved inside that folder. By default this will not overwrite an existing destination.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_path": {
                        "type": "string",
                        "description": "Current file or folder path, e.g.: workspace/report.md or workspace/presentations/deck.pptx",
                    },
                    "destination_path": {
                        "type": "string",
                        "description": "Destination file/folder path, e.g.: workspace/archive/report.md or workspace/presentations/PPT/",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "Replace the destination if it already exists. Default false.",
                    },
                },
                "required": ["source_path", "destination_path"],
            },
        },
    },
    # --- Enhanced file management tools ---
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Surgically replace a specific string inside an existing file without rewriting the whole content. Prefer this over write_file when you only need to change one or more sections - it avoids accidentally overwriting content outside the edit target and is safer in multi-agent scenarios. enterprise_info/ is shared company context and is read-only for agents. The old_string must match exactly (including all whitespace and newlines).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path to edit, e.g.: memory/memory.md, skills/my-skill/SKILL.md",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact text to find and replace. Must match exactly including whitespace and newlines.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace all occurrences if true (default: false). Set to true when you want to replace every match.",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for content patterns across files using regex. Returns matching lines with file paths and line numbers. Useful for finding code, configurations, or text across the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for, e.g.: 'API_KEY', 'def\\\\s+\\\\w+', '@app\\\\.(get|post)'",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in (default: root). e.g.: 'skills', 'workspace', 'memory'",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "File pattern to match (default: all files). e.g.: '*.md', '*.py', '*.json'",
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "Case-insensitive search (default: false)",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "Find files matching glob patterns. Returns file paths with sizes and modification info. Useful for discovering files in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern to match files, e.g.: '**/*.md' (all markdown files), 'skills/*.md' (skill files), 'workspace/**/*' (all files under workspace)",
                    },
                    "path": {
                        "type": "string",
                        "description": "Base directory for search (default: root). e.g.: 'skills', 'workspace'",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    # --- Trigger management tools (Aware engine) ---
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
    {
        "type": "function",
        "function": {
            "name": "jina_search",
            "description": "Search the internet using Jina AI Search (s.jina.ai). Returns high-quality search results with full page content, not just snippets. Ideal for research, news, technical docs, and any real-time information lookup.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query, e.g. 'Python asyncio best practices' or '苏州通道人工智能科技有限公司'",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return, default 5, max 10",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "jina_read",
            "description": "Read and extract the full content from a web page URL using Jina AI Reader (r.jina.ai). Returns clean, well-structured markdown including article text, tables, and key information. Better than jina_search when you already have a specific URL to read.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL of the web page to read, e.g. 'https://example.com/article'",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max characters to return (default 8000, max 20000)",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_webpage",
            "description": "Fetch a public HTTP/HTTPS URL directly and extract readable webpage text. Use this when you already have a specific link and need the page content without relying on an external reader service. Private, local, and internal network URLs are blocked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full public HTTP/HTTPS URL of the web page to read, e.g. 'https://example.com/article'",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max characters to return (default 12000, max 50000)",
                    },
                    "include_links": {
                        "type": "boolean",
                        "description": "Include up to 30 extracted page links (default false)",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "Read office document contents (PDF, Word, Excel, PPT, etc.) and extract text. Suitable for reading knowledge base documents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Document file path, e.g.: workspace/knowledge_base/report.pdf, enterprise_info/knowledge_base/policy.docx",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_code",
            "description": "Execute code (Python, Bash, or Node.js) in a local sandboxed subprocess within the agent's root directory. Useful for data processing, calculations, file transformations, and automation scripts. Code runs with the agent root as the working directory, so you can access skills/, workspace/, memory/ etc. directly. Security restrictions apply: no system-level operations, 30-second default timeout.",
            "parameters": {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["python", "bash", "node"],
                        "description": "Programming language to execute",
                    },
                    "code": {
                        "type": "string",
                        "description": "Code to execute. If a Python import fails due to a missing package, install it first via execute_code with language='bash' and code='pip install <package>'. Working directory is the agent root (skills/, workspace/, memory/ are accessible).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max execution time in seconds (default 60, max 3600)",
                    },
                },
                "required": ["language", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_code_e2b",
            "description": "Execute code (Python, Bash, or Node.js) in a secure E2B cloud sandbox. The sandbox has full network access and is fully isolated from the server. Use this when local execution is insufficient or when network access is required inside the code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["python", "bash", "node"],
                        "description": "Programming language to execute",
                    },
                    "code": {
                        "type": "string",
                        "description": "Code to execute in the E2B cloud sandbox.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max execution time in seconds (default 30, max 60)",
                    },
                },
                "required": ["language", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upload_image",
            "description": "Upload an image file from your workspace (or from a public URL) to a cloud CDN and get a permanent public URL. Use this when you need to share images externally, embed them in messages/reports, or make workspace images accessible via URL. Supports common formats: PNG, JPG, GIF, WebP, SVG.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Workspace-relative path to the image file, e.g. workspace/chart.png or workspace/knowledge_base/diagram.jpg",
                    },
                    "url": {
                        "type": "string",
                        "description": "Alternative: a public URL of an image to upload (e.g. https://example.com/photo.jpg). Use this instead of file_path when the image is not in your workspace.",
                    },
                    "file_name": {
                        "type": "string",
                        "description": "Optional custom filename for the uploaded image. If omitted, the original filename is used.",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Optional CDN folder path, e.g. /agents/reports. Defaults to /maraclaw.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image_siliconflow",
            "description": "Generate an image via SiliconFlow (FLUX). Save to workspace. Fast and China-friendly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed image description in English.",
                    },
                    "size": {
                        "type": "string",
                        "description": "Image size. Default: 1024x1024. Options: 1024x1024, 1024x768, 768x1024",
                    },
                    "save_path": {
                        "type": "string",
                        "description": "Workspace path to save the image (e.g. workspace/images/sunset.png).",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image_openai",
            "description": "Generate an image via OpenAI. Save to workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed image description in English.",
                    },
                    "size": {
                        "type": "string",
                        "description": "Image size. Default: 1024x1024.",
                    },
                    "save_path": {
                        "type": "string",
                        "description": "Workspace path to save the image.",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image_google",
            "description": "Generate an image via Google Gemini Image (Nano Banana) or Vertex AI. Save to workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed image description in English.",
                    },
                    "size": {
                        "type": "string",
                        "description": "Image size. Default: 1024x1024.",
                    },
                    "save_path": {
                        "type": "string",
                        "description": "Workspace path to save the image.",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image_custom",
            "description": "Generate an image via the company-configured custom image API. Save to workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed image description in English.",
                    },
                    "size": {
                        "type": "string",
                        "description": "Image size. Default: 1024x1024.",
                    },
                    "save_path": {
                        "type": "string",
                        "description": "Workspace path to save the image.",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discover_resources",
            "description": "Search public MCP registries (Smithery) for tools and capabilities that can extend your abilities. Use this when you encounter a task you cannot handle with your current tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Semantic description of the capability needed, e.g. 'send email', 'query SQL database', 'generate images'",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max results to return (default 5, max 10)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    # ── Feishu Bitable (多维表格) Tools ──────────────────────
    {
        "type": "function",
        "function": {
            "name": "bitable_list_tables",
            "description": "列出飞书多维表格内的所有数据表 (Tables)。url 支持表格链接或 Wiki 链接。使用此工具了解请求的多维表格中有哪些表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "多维表格的 URL 链接。"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bitable_list_fields",
            "description": "列出飞书多维表格指定数据表中的所有字段 (Fields)。url 支持表格链接或 Wiki 链接。在查询或修改数据前，必须先调用此工具了解字段名称和类型。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "多维表格的 URL 链接。"},
                    "table_id": {"type": "string", "description": "具体的数据表 ID，如果 url 中包含 tbl 则可以不填。"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bitable_query_records",
            "description": "查询飞书多维表格中的数据行。可以提供过滤条件 (filter)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "多维表格的 URL 链接。"},
                    "table_id": {"type": "string", "description": "具体的数据表 ID，如果 url 中包含 tbl 则可以不填。"},
                    "filter_info": {
                        "type": "string",
                        "description": "可选，FQL 语法的过滤条件，例如 'CurrentValue.[Status]=\"Done\"'。如不确定过滤语法，可以不填，由你臺己在本地过滤返回的所有数据。",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最大返回条数 (默认 100)",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bitable_create_record",
            "description": "在飞书多维表格中新增一行数据。fields 参数是一个字典，key 是字段名 (需要先通过 bitable_list_fields 获取)，value 是对应的值。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "多维表格的 URL 链接。"},
                    "table_id": {"type": "string", "description": "具体的数据表 ID，如果 url 中包含 tbl 则可以不填。"},
                    "fields": {
                        "type": "string",
                        "description": '一个 JSON 字符串，代表要插入的 fields。例如：\'{"Name": "张三", "Age": 30}\'',
                    },
                },
                "required": ["url", "fields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bitable_update_record",
            "description": "更新飞书多维表格中的指定行数据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "多维表格的 URL 链接。"},
                    "table_id": {"type": "string", "description": "具体的数据表 ID，如果 url 中包含 tbl 则可以不填。"},
                    "record_id": {
                        "type": "string",
                        "description": "要更新的 record_id，通过 bitable_query_records 获取。",
                    },
                    "fields": {
                        "type": "string",
                        "description": '一个 JSON 字符串，代表要更新的 fields。例如：\'{"Status": "Done"}\'',
                    },
                },
                "required": ["url", "record_id", "fields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bitable_delete_record",
            "description": "删除飞书多维表格中的指定行数据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "多维表格的 URL 链接。"},
                    "table_id": {"type": "string", "description": "具体的数据表 ID，如果 url 中包含 tbl 则可以不填。"},
                    "record_id": {
                        "type": "string",
                        "description": "要删除的 record_id，通过 bitable_query_records 获取。",
                    },
                },
                "required": ["url", "record_id"],
            },
        },
    },
    # ── Feishu Document Tools ──────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "feishu_doc_search",
            "description": (
                "Search Feishu cloud documents by keyword using the official Feishu document search API. "
                + "Use this when a wiki folder or knowledge base contains too many documents for feishu_wiki_list to be practical. "
                + "Returns matching titles, document tokens, and document types so you can then read, share, or delete the target file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keyword, e.g. '恩菲', '客户周报', or '项目章程'",
                    },
                    "docs_types": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["doc", "docx", "sheet", "bitable", "file", "folder", "mindnote", "slides"],
                        },
                        "description": "Optional file type filter. Omit to search across all supported Feishu document types.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results to return (default 10, max 50).",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Result offset for pagination (default 0).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_wiki_list",
            "description": (
                "List all sub-pages (child nodes) of a Feishu Wiki (知识库) page. "
                + "Works with wiki URLs like 'https://xxx.feishu.cn/wiki/NodeToken'. "
                + "Use this when a wiki page has child pages you need to explore. "
                + "Returns titles, node_tokens, and obj_tokens for each sub-page. "
                + "Each sub-page can then be read with feishu_doc_read using its node_token."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "node_token": {
                        "type": "string",
                        "description": "Wiki node token from the URL, e.g. 'HrGawgXxLiqoS5kT6pUczya3nEc' from 'https://xxx.feishu.cn/wiki/HrGawgXxLiqoS5kT6pUczya3nEc'",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "If true, also list sub-pages of sub-pages (up to 2 levels deep). Default false.",
                    },
                },
                "required": ["node_token"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_doc_read",
            "description": (
                "Read the text content of a Feishu document or Wiki page. "
                + "Works with both regular docx URLs (https://xxx.feishu.cn/docx/Token) "
                + "and Wiki page URLs (https://xxx.feishu.cn/wiki/Token). "
                + "Automatically handles wiki node tokens. "
                + "If the page has sub-pages, use feishu_wiki_list to list them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_token": {
                        "type": "string",
                        "description": "Feishu document token (from document URL)",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max characters to return (default 6000, max 20000)",
                    },
                },
                "required": ["document_token"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_doc_create",
            "description": "Create a new Feishu document. Supports creating in personal Drive (default) or directly inside a Wiki knowledge base (provide wiki_space_id). Returns the document token and URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Document title",
                    },
                    "folder_token": {
                        "type": "string",
                        "description": "Optional: parent folder token in Drive. Leave empty to create in root My Drive. Ignored when wiki_space_id is provided.",
                    },
                    "wiki_space_id": {
                        "type": "string",
                        "description": "Optional: Wiki space ID. When provided, creates the document as a node inside this Wiki space instead of personal Drive. Get this from feishu_wiki_list or from the wiki URL.",
                    },
                    "parent_node_token": {
                        "type": "string",
                        "description": "Optional: parent node token within the Wiki space. When provided together with wiki_space_id, creates the document under this specific wiki node. If omitted, creates at the wiki space root.",
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_doc_append",
            "description": "Append text content to an existing Feishu document. Content is appended as one or more new paragraphs at the end.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_token": {
                        "type": "string",
                        "description": "Feishu document token",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to append. Supports multiple lines separated by \\n.",
                    },
                },
                "required": ["document_token", "content"],
            },
        },
    },
    # ── Feishu Calendar Tools ──────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "feishu_calendar_list",
            "description": "查询飞书日历。**自动读取当前对话用户的真实忙碌时段（freebusy）**，同时列出 bot 创建的日程。用于查询某人是否有空、安排日程时避开冲突。",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_time": {
                        "type": "string",
                        "description": "查询起始时间，ISO 8601 格式，例如 '2026-03-13T00:00:00+08:00'。默认：当前时间。",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "查询截止时间，ISO 8601 格式。默认：7天后。",
                    },
                    "user_open_id": {
                        "type": "string",
                        "description": "要查询 freebusy 的用户 open_id。不填则自动使用当前对话发送者。",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max events to return (default 20)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_calendar_create",
            "description": "Create a Feishu calendar event immediately. The current user is automatically invited as attendee - no email or authorization required. Just provide the title and time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Event title",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Event start in ISO 8601 with timezone, e.g. '2026-03-15T14:00:00+08:00'",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "Event end in ISO 8601 with timezone, e.g. '2026-03-15T15:00:00+08:00'",
                    },
                    "description": {
                        "type": "string",
                        "description": "Event description or agenda",
                    },
                    "attendee_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names of colleagues to invite, e.g. ['覃睿', '张三']. Will be looked up automatically via feishu_user_search.",
                    },
                    "attendee_open_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Feishu open_ids to invite directly (if you already have them from feishu_user_search).",
                    },
                    "attendee_emails": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional attendee emails to invite (use attendee_names if you only have the name).",
                    },
                    "location": {
                        "type": "string",
                        "description": "Event location or meeting room",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "Timezone, e.g. 'Asia/Shanghai'. Defaults to Asia/Shanghai.",
                    },
                },
                "required": ["summary", "start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_calendar_update",
            "description": "Update an existing Feishu calendar event. Provide only the fields you want to change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_email": {"type": "string", "description": "Calendar owner's email"},
                    "event_id": {"type": "string", "description": "Event ID from feishu_calendar_list"},
                    "summary": {"type": "string", "description": "New title"},
                    "description": {"type": "string", "description": "New description"},
                    "start_time": {"type": "string", "description": "New start time (ISO 8601)"},
                    "end_time": {"type": "string", "description": "New end time (ISO 8601)"},
                    "location": {"type": "string", "description": "New location"},
                },
                "required": ["user_email", "event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_calendar_delete",
            "description": "Delete (cancel) a Feishu calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_email": {"type": "string", "description": "Calendar owner's email"},
                    "event_id": {"type": "string", "description": "Event ID to delete"},
                },
                "required": ["user_email", "event_id"],
            },
        },
    },
    # ── Feishu Drive Share (collaborator management for all file types) ──
    {
        "type": "function",
        "function": {
            "name": "feishu_drive_share",
            "description": (
                "Manage Feishu Drive file collaborators and permissions. "
                + "Supports ALL file types: docx, bitable, sheet, doc, folder, mindnote, slides. "
                + "Can add or remove collaborators with viewer/editor/full_access roles, "
                + "or get the current collaborator list. "
                + "Accepts colleague names (auto-searched) or open_ids directly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_token": {
                        "type": "string",
                        "description": "File token (from feishu_doc_create, bitable_create_app, or URL)",
                    },
                    "doc_type": {
                        "type": "string",
                        "enum": ["docx", "bitable", "sheet", "doc", "folder", "mindnote", "slides"],
                        "description": "File type. Default: 'docx'. Use 'bitable' for Bitable, 'sheet' for Spreadsheet, etc.",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "list"],
                        "description": "'add' to grant access, 'remove' to revoke, 'list' to view current collaborators",
                    },
                    "member_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Colleague names to add/remove, e.g. ['覃睿', '张三']. Auto-searched.",
                    },
                    "member_open_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Feishu open_ids to add/remove directly (if already known).",
                    },
                    "permission": {
                        "type": "string",
                        "enum": ["view", "edit", "full_access"],
                        "description": "Permission level: 'view' (read-only), 'edit' (can edit), 'full_access' (can manage). Default: 'edit'",
                    },
                },
                "required": ["document_token", "action"],
            },
        },
    },
    # ── Feishu Drive Delete (delete files from cloud space) ──
    {
        "type": "function",
        "function": {
            "name": "feishu_drive_delete",
            "description": (
                "Delete a file or folder from Feishu Drive (cloud space). "
                + "The file will be moved to the recycle bin, not permanently deleted. "
                + "For folders, the deletion is asynchronous. "
                + "Requires ownership + parent folder edit permission, or parent folder full_access."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_token": {
                        "type": "string",
                        "description": "Token of the file or folder to delete (from URL or previous tool output)",
                    },
                    "file_type": {
                        "type": "string",
                        "enum": ["file", "docx", "bitable", "folder", "doc", "sheet", "mindnote", "shortcut", "slides"],
                        "description": "Type of the file to delete. Use 'docx' for documents, 'bitable' for multitable, 'sheet' for spreadsheets, 'file' for uploaded files, 'folder' for folders.",
                    },
                },
                "required": ["file_token", "file_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_user_search",
            "description": (
                "Search for a colleague in the Feishu (Lark) directory by name. "
                + "Returns their open_id, email, and department so you can send messages, "
                + "invite them to calendar events, or share documents. "
                + "Use this whenever you need to find a colleague's Feishu identity."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The colleague's name to search for, e.g. '覃睿' or '张三'",
                    },
                },
                "required": ["name"],
            },
        },
    },
    # ── Feishu Approval Tools ──────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "feishu_approval_create",
            "description": "发起一个飞书审批流实例。你需要知道审批定义的 approval_code 和表单对应字段的内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "approval_code": {
                        "type": "string",
                        "description": "审批定义的唯一代码 (approval_code)",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "发起人的 open_id。可以通过 feishu_user_search 获取。",
                    },
                    "form_data": {
                        "type": "string",
                        "description": '表单内容的 JSON 字符串，例如 \'[{"id":"widget1","type":"input","value":"这是内容"}]\'',
                    },
                },
                "required": ["approval_code", "user_id", "form_data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_approval_query",
            "description": "查询指定的飞书审批实例列表。可以支持按状态查询（PENDING, APPROVED, REJECTED, CANCELED, DELETED）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "approval_code": {
                        "type": "string",
                        "description": "审批定义的唯一代码 (approval_code)",
                    },
                    "status": {
                        "type": "string",
                        "description": "可选过滤状态：PENDING, APPROVED, REJECTED, CANCELED, DELETED",
                    },
                },
                "required": ["approval_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_approval_get",
            "description": "获取指定飞书审批实例的详细信息与当前审批状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "instance_id": {
                        "type": "string",
                        "description": "审批实例的 instance_id",
                    },
                },
                "required": ["instance_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "import_mcp_server",
            "description": "Import an MCP server from Smithery registry into the platform. The server's tools become available for use. Use discover_resources first to find the server ID. If previously imported tools stopped working (e.g. OAuth expired), set reauthorize=true to re-run the authorization flow.",
            "parameters": {
                "type": "object",
                "properties": {
                    "server_id": {
                        "type": "string",
                        "description": "Smithery server ID, e.g. '@anthropic/brave-search' or '@anthropic/fetch'",
                    },
                    "config": {
                        "type": "object",
                        "description": "Optional server configuration (e.g. API keys required by the server)",
                    },
                    "reauthorize": {
                        "type": "boolean",
                        "description": "Set to true to force re-authorization of existing tools (e.g. when OAuth token has expired)",
                    },
                },
                "required": ["server_id"],
            },
        },
    },
    # ─── Email Tools ────────────────────────
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
    # --- Pages: public HTML hosting ---
    {
        "type": "function",
        "function": {
            "name": "publish_page",
            "description": "Publish an HTML file from workspace as a public page. Returns a public URL that anyone can access without login. Only .html/.htm files can be published.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path in workspace, e.g. 'workspace/output.html'",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_published_pages",
            "description": "List all pages published by this agent, showing their public URLs and view counts.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    # --- Skill Management ---
    {
        "type": "function",
        "function": {
            "name": "search_clawhub",
            "description": "Search the ClawHub skill registry for skills matching a query. Returns a list of available skills with name, description, and last updated date. Use this to help users find skills to install.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query, e.g. 'research', 'code review', 'market analysis'",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_skill",
            "description": "Install a skill into this agent's workspace. Accepts either a ClawHub skill slug (e.g. 'market-research') or a GitHub URL (e.g. 'https://github.com/user/repo'). The skill files will be downloaded and saved to skills/<name>/ in your workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "ClawHub skill slug (e.g. 'market-research') or GitHub URL (e.g. 'https://github.com/user/repo')",
                    },
                },
                "required": ["source"],
            },
        },
    },
    # ── AgentBay Tools ────────────────────────────────────────────
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


# Core tools that should always be available to agents regardless of
# DB configuration.
# Note: send_channel_message is intentionally NOT here - it lives in
# _CHANNEL_MESSAGE_TOOL_NAMES and is only added when a channel is configured,
# to avoid sending duplicate tool definitions to the LLM.
_ALWAYS_INCLUDE_CORE: set[str] = {
    "complete_focus_item",
    FINISH_TOOL_NAME,
    "list_focus_items",
    "send_channel_file",
    "send_file_to_agent",
    "upsert_focus_item",
    "write_file",
}
# Channel message tool - available when any channel (Feishu/DingTalk/WeCom) is configured
_CHANNEL_MESSAGE_TOOL_NAMES: set[str] = {
    "send_channel_message",
}
# Feishu tools are ONLY included when the agent has a configured Feishu channel,
# to avoid exposing unnecessary tools to non-Feishu agents (reduces hallucination risk).
_FEISHU_TOOL_NAMES: set[str] = {
    "send_feishu_message",
    "feishu_user_search",
    # Seeded/configured Feishu tool name retained for DB gating; no static definition exists.
    "bitable_create_app",
    "bitable_list_tables",
    "bitable_list_fields",
    "bitable_query_records",
    "bitable_create_record",
    "bitable_update_record",
    "bitable_delete_record",
    "feishu_doc_search",
    "feishu_wiki_list",
    "feishu_doc_read",
    "feishu_doc_create",
    "feishu_doc_append",
    "feishu_drive_share",
    "feishu_drive_delete",
    "feishu_calendar_list",
    "feishu_calendar_create",
    "feishu_calendar_update",
    "feishu_calendar_delete",
    "feishu_approval_create",
    "feishu_approval_query",
    "feishu_approval_get",
}


def _catalog_tool_name(tool: object) -> str:
    if not isinstance(tool, dict):
        return ""
    function = tool.get("function")
    if not isinstance(function, dict):
        return ""
    name = function.get("name")
    return name if isinstance(name, str) else ""


_always_core_tools: list[object] = [t for t in AGENT_TOOLS if _catalog_tool_name(t) in _ALWAYS_INCLUDE_CORE]
_feishu_tools: list[object] = [t for t in AGENT_TOOLS if _catalog_tool_name(t) in _FEISHU_TOOL_NAMES]
_channel_tools: list[object] = [t for t in AGENT_TOOLS if _catalog_tool_name(t) in _CHANNEL_MESSAGE_TOOL_NAMES]
