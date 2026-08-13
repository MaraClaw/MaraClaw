"""Composed builtin tool definitions."""

# ruff: noqa: RUF001

from app.services.llm.finish import FINISH_TOOL_SEED
from app.services.tool_definitions.agentbay import AGENTBAY_TOOLS
from app.services.tool_definitions.deploy import DEPLOY_BUILTIN_TOOLS
from app.services.tool_definitions.okr import OKR_BUILTIN_TOOLS

# Builtin tool definitions - these map to the hardcoded AGENT_TOOLS
BUILTIN_TOOLS = [
    FINISH_TOOL_SEED,
    {
        "name": "list_files",
        "display_name": "List Files",
        "description": "List files and folders in a directory within the workspace. Use this before writing new workspace documents so you can inspect the current folder structure, reuse existing topical subfolders when appropriate, and avoid dumping files directly into the workspace root unless there is a clear reason. Can also list enterprise_info/ for shared company information.",
        "category": "file",
        "icon": "📁",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to list, defaults to root (empty string)"}
            },
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "read_file",
        "display_name": "Read File",
        "description": "Read file contents from the workspace. Can read soul.md, memory/memory.md, skills/, and enterprise_info/. Focus is stored in system tools, not focus.md. Use offset and limit for reading large files in chunks.",
        "category": "file",
        "icon": "📄",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path, e.g.: soul.md, memory/memory.md"},
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
        "config": {"max_file_size_kb": 500},
        "config_schema": {
            "fields": [
                {"key": "max_file_size_kb", "label": "Max file size (KB)", "type": "number", "default": 500},
            ]
        },
    },
    {
        "name": "list_focus_items",
        "display_name": "List Focus Items",
        "description": "List structured Focus items from the system database.",
        "category": "file",
        "icon": "◎",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "include_completed": {
                    "type": "boolean",
                    "description": "Whether to include completed Focus items. Default true.",
                },
            },
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "upsert_focus_item",
        "display_name": "Upsert Focus Item",
        "description": "Create or update a structured Focus item in the system database.",
        "category": "file",
        "icon": "◎",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Stable short identifier, snake_case preferred."},
                "title": {"type": "string", "description": "Short title (Focus name)."},
                "description": {
                    "type": "string",
                    "description": "Human-readable description of what is being tracked.",
                },
                "kind": {"type": "string", "enum": ["normal", "system"], "description": "normal or system"},
                "source": {"type": "string", "description": "Optional origin label, e.g. user, trigger, a2a, okr."},
            },
            "required": ["description"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "complete_focus_item",
        "display_name": "Complete Focus Item",
        "description": "Mark a structured Focus item completed.",
        "category": "file",
        "icon": "◎",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Focus item identifier to complete."},
            },
            "required": ["key"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "write_file",
        "display_name": "Write File",
        "description": "Write or update a file in the workspace. Before creating a new document under workspace/, first inspect the relevant directories with list_files, prefer an existing topical subfolder over the workspace root, and create a new subfolder when the content belongs to a new category. Avoid placing standalone document files directly in workspace/ root unless the user explicitly wants that. Can update memory/memory.md, create documents in workspace/, create skills in skills/.",
        "category": "file",
        "icon": "✏️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path, e.g.: memory/memory.md, workspace/reports/report.md, workspace/knowledge_base/notes.md. Prefer a meaningful subfolder instead of writing loose files into workspace/ root.",
                },
                "content": {"type": "string", "description": "File content to write"},
            },
            "required": ["path", "content"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "delete_file",
        "display_name": "Delete File",
        "description": "Delete a file from the workspace. Cannot delete soul.md or tasks.json.",
        "category": "file",
        "icon": "🗑️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path to delete"}},
            "required": ["path"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "move_file",
        "display_name": "Move File",
        "description": "Move or rename a file or folder within the workspace. Use this instead of execute_code for reorganizing workspace files, moving generated documents into subfolders, or renaming files. Cannot move soul.md, tasks.json, or enterprise_info/. If destination_path is an existing folder or ends with '/', the original filename is preserved inside that folder. Does not overwrite by default.",
        "category": "file",
        "icon": "↪",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "Current file or folder path, e.g.: workspace/report.md",
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
        "config": {},
        "config_schema": {},
    },
    # --- Enhanced file management tools ---
    {
        "name": "edit_file",
        "display_name": "Edit File",
        "description": "Surgically replace a specific string inside an existing file without rewriting the whole content. Prefer this over write_file when you only need to change one or more sections.",
        "category": "file",
        "icon": "✂️",
        "is_default": True,
        "parameters_schema": {
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
                "new_string": {"type": "string", "description": "Replacement text"},
                "replace_all": {"type": "boolean", "description": "Replace all occurrences if true (default: false)"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "search_files",
        "display_name": "Search Files",
        "description": "Search for content patterns across files using regex. Returns matching lines with file paths and line numbers. Results capped at 50 per query.",
        "category": "file",
        "icon": "🔍",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for, e.g.: 'API_KEY', 'def\\\\s+\\\\w+'",
                },
                "path": {"type": "string", "description": "Directory to search in (default: root)"},
                "file_pattern": {
                    "type": "string",
                    "description": "File pattern to match (default: all files). e.g.: '*.md', '*.py'",
                },
                "ignore_case": {"type": "boolean", "description": "Case-insensitive search (default: false)"},
            },
            "required": ["pattern"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "find_files",
        "display_name": "Find Files",
        "description": "Find files matching glob patterns. Returns file paths with sizes and modification info. Results capped at 100 per query.",
        "category": "file",
        "icon": "📁",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match files, e.g.: '**/*.md', 'skills/*.md'",
                },
                "path": {"type": "string", "description": "Base directory for search (default: root)"},
            },
            "required": ["pattern"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "read_document",
        "display_name": "Read Document",
        "description": "Read office document contents (PDF, Word, Excel, PPT) and extract text.",
        "category": "file",
        "icon": "📑",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Document file path, e.g.: workspace/report.pdf"}},
            "required": ["path"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "convert_csv_to_xlsx",
        "display_name": "CSV to Excel",
        "description": "Convert a CSV source file into an Excel .xlsx file. Create/edit the CSV first, then use this tool.",
        "category": "file",
        "icon": "📊",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Path to the source CSV file"},
                "target_path": {"type": "string", "description": "Path for the output Excel file (.xlsx)"},
            },
            "required": ["source_path", "target_path"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "convert_html_to_pdf",
        "display_name": "HTML to PDF",
        "description": "Convert an HTML source file into a PDF document. Uses headless Chrome by default for higher-fidelity rendering of modern CSS and screen layouts, with WeasyPrint as a fallback.",
        "category": "file",
        "icon": "📄",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Path to the source HTML file"},
                "target_path": {"type": "string", "description": "Path for the output PDF file (.pdf)"},
                "design_width": {
                    "type": "number",
                    "description": "Optional browser viewport width in pixels, default 1280",
                },
                "design_height": {
                    "type": "number",
                    "description": "Optional browser viewport height in pixels, default 720",
                },
                "pdf_mode": {
                    "type": "string",
                    "enum": ["pages", "single"],
                    "description": "pages outputs paginated PDF, single outputs one long full-page PDF. Default: pages",
                },
                "scale": {
                    "type": "number",
                    "description": "Optional Chrome PDF scale for paginated output, default 0.64",
                },
                "paper_width": {
                    "type": "number",
                    "description": "Optional paper width in inches for paginated output, default 8.27",
                },
                "paper_height": {
                    "type": "number",
                    "description": "Optional paper height in inches for paginated output, default 11.69",
                },
            },
            "required": ["source_path", "target_path"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "convert_html_to_pptx",
        "display_name": "HTML to PowerPoint",
        "description": "Convert an HTML source file into a PowerPoint .pptx file. By default, render_mode='editable' opens the HTML in headless Chrome, samples real element positions/styles, and maps explicit .slide/data-slide nodes or top-level page sections into editable PPT elements. Use render_mode='visual' as a high-fidelity screenshot fallback when exact visual preservation is more important than editability.",
        "category": "file",
        "icon": "📽️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Path to the source HTML file"},
                "target_path": {"type": "string", "description": "Path for the output PowerPoint file (.pptx)"},
                "design_width": {
                    "type": "number",
                    "description": "Optional source design width in pixels, default 1280",
                },
                "design_height": {
                    "type": "number",
                    "description": "Optional source design height in pixels, default 720",
                },
                "render_mode": {
                    "type": "string",
                    "enum": ["editable", "visual"],
                    "description": "editable maps HTML/CSS into editable PPT elements using Chrome layout sampling; visual preserves styling with Chrome-rendered screenshots as a fallback. Default: editable",
                },
                "render_scale": {
                    "type": "number",
                    "description": "Optional Chrome raster scale for screenshots and complex CSS captures. Higher values improve sharpness but increase PPTX size. Default: 2, clamped between 1 and 4",
                },
            },
            "required": ["source_path", "target_path"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "convert_markdown_to_docx",
        "display_name": "Markdown to Word",
        "description": "Convert a Markdown source file into a Word .docx file.",
        "category": "file",
        "icon": "📝",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Path to the source Markdown file"},
                "target_path": {"type": "string", "description": "Path for the output Word file (.docx)"},
            },
            "required": ["source_path", "target_path"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "convert_markdown_to_pdf",
        "display_name": "Markdown to PDF",
        "description": "Convert a Markdown source file into a PDF document.",
        "category": "file",
        "icon": "📄",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Path to the source Markdown file"},
                "target_path": {"type": "string", "description": "Path for the output PDF file (.pdf)"},
            },
            "required": ["source_path", "target_path"],
        },
        "config": {},
        "config_schema": {},
    },
    # --- Aware trigger management tools ---
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
    # NOTE: send_feishu_message is defined in the 'feishu' category section below.
    # It was previously duplicated here under 'communication', which could cause
    # 'Tool names must be unique' errors when the DB lacked a UNIQUE constraint.
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
    {
        "name": "web_search",
        "display_name": "Web Search",
        "description": "[Deprecated] Unified search tool with engine selector. Use the dedicated tools (DuckDuckGo Search, Tavily Search, Google Search, Bing Search, Exa Search) instead for better control per engine.",
        "category": "search",
        "icon": "🔍",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
                "max_results": {"type": "integer", "description": "Number of results to return"},
            },
            "required": ["query"],
        },
        "config": {
            "search_engine": "duckduckgo",
            "max_results": 5,
            "language": "en",
            "api_key": "",
        },
        "config_schema": {
            "fields": [
                {
                    "key": "search_engine",
                    "label": "Search Engine",
                    "type": "select",
                    "options": [
                        {"value": "duckduckgo", "label": "DuckDuckGo (free, no API key)"},
                        {"value": "tavily", "label": "Tavily (AI search, needs API key)"},
                        {"value": "google", "label": "Google Custom Search (needs API key)"},
                        {"value": "bing", "label": "Bing Search API (needs API key)"},
                        {"value": "exa", "label": "Exa (AI-powered search, needs API key)"},
                    ],
                    "default": "duckduckgo",
                },
                {
                    "key": "api_key",
                    "label": "API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "Required for engines that need an API key",
                    "depends_on": {"search_engine": ["tavily", "google", "bing", "exa"]},
                },
                {
                    "key": "max_results",
                    "label": "Default results count",
                    "type": "number",
                    "default": 5,
                    "min": 1,
                    "max": 20,
                },
                {
                    "key": "language",
                    "label": "Search language",
                    "type": "select",
                    "options": [
                        {"value": "en", "label": "English"},
                        {"value": "zh-CN", "label": "Chinese"},
                        {"value": "ja", "label": "Japanese"},
                    ],
                    "default": "en",
                },
            ]
        },
    },
    {
        "name": "jina_search",
        "display_name": "Jina Search",
        "description": "Search the internet using Jina AI (s.jina.ai). Returns high-quality results with full content. Requires Jina AI API key for higher rate limits.",
        "category": "search",
        "icon": "🔮",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
                "max_results": {"type": "integer", "description": "Number of results (default 5, max 10)"},
            },
            "required": ["query"],
        },
        "config": {},
        "config_schema": {
            "fields": [
                {
                    "key": "api_key",
                    "label": "Jina AI API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "jina_xxxxxxxxxxxxxxxx (get one at jina.ai)",
                },
            ]
        },
    },
    {
        "name": "jina_read",
        "display_name": "Jina Read",
        "description": "Read and extract full content from a URL using Jina AI Reader (r.jina.ai). Returns clean markdown. Requires Jina AI API key for higher rate limits.",
        "category": "search",
        "icon": "📖",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to read"},
                "max_chars": {"type": "integer", "description": "Max characters to return (default 8000)"},
            },
            "required": ["url"],
        },
        "config": {},
        "config_schema": {
            "fields": [
                {
                    "key": "api_key",
                    "label": "Jina AI API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "jina_xxxxxxxxxxxxxxxx (get one at jina.ai)",
                },
            ]
        },
    },
    {
        "name": "read_webpage",
        "display_name": "Read Webpage",
        "description": "Fetch a public HTTP/HTTPS URL directly and extract readable webpage text. Use this when you already have a specific link and need its page content without relying on an external reader service.",
        "category": "search",
        "icon": "🌐",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full public HTTP/HTTPS URL to read"},
                "max_chars": {"type": "integer", "description": "Max characters to return (default 12000, max 50000)"},
                "include_links": {
                    "type": "boolean",
                    "description": "Whether to include extracted page links (default false)",
                },
            },
            "required": ["url"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "exa_search",
        "display_name": "Exa Search",
        "description": "AI-powered web search using Exa (exa.ai). Supports semantic search, category filtering, domain filtering, and multiple content modes (text, highlights, summary). Requires an Exa API key.",
        "category": "search",
        "icon": "🔎",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Number of results (default 5, max 10)"},
                "search_type": {
                    "type": "string",
                    "description": "Search type: auto (default), neural, or fast",
                    "enum": ["auto", "neural", "fast"],
                },
                "category": {
                    "type": "string",
                    "description": "Filter by category: company, research paper, news, personal site, financial report, or people",
                },
                "include_domains": {
                    "type": "string",
                    "description": "Comma-separated domains to restrict results to (e.g. 'arxiv.org, github.com')",
                },
                "exclude_domains": {
                    "type": "string",
                    "description": "Comma-separated domains to exclude from results",
                },
                "content_mode": {
                    "type": "string",
                    "description": "Content retrieval mode: text (default), highlights, or summary",
                    "enum": ["text", "highlights", "summary"],
                },
            },
            "required": ["query"],
        },
        "config": {},
        "config_schema": {
            "fields": [
                {
                    "key": "api_key",
                    "label": "Exa API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "Get your API key at exa.ai",
                },
            ]
        },
    },
    # ── Standalone search engines (each engine as its own tool) ──────────────
    # These complement web_search (which remains for backward compatibility).
    # Each tool wraps a single engine so agents can pick the right one for the
    # task without going through the unified engine-selector flow.
    {
        "name": "duckduckgo_search",
        "display_name": "DuckDuckGo Search",
        "description": "Search the internet using DuckDuckGo. Free, no API key required. Returns titles, URLs, and snippets.",
        "category": "search",
        "icon": "🦆",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
                "max_results": {"type": "integer", "description": "Number of results to return (default 5, max 10)"},
            },
            "required": ["query"],
        },
        "config": {},
        "config_schema": {"fields": []},
    },
    {
        "name": "tavily_search",
        "display_name": "Tavily Search",
        "description": "AI-optimized web search using Tavily. Returns high-quality results with summaries. Requires a Tavily API key.",
        "category": "search",
        "icon": "🔍",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
                "max_results": {"type": "integer", "description": "Number of results to return (default 5, max 10)"},
            },
            "required": ["query"],
        },
        "config": {},
        "config_schema": {
            "fields": [
                {
                    "key": "api_key",
                    "label": "Tavily API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "tvly-xxxxxxxxxxxxxxxx (get one at tavily.com)",
                },
            ]
        },
    },
    {
        "name": "google_search",
        "display_name": "Google Search",
        "description": "Search using Google Custom Search JSON API. Returns titles, URLs, and snippets. Requires a Google API key and Custom Search Engine ID (format: API_KEY:CX_ID).",
        "category": "search",
        "icon": "🔍",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
                "max_results": {"type": "integer", "description": "Number of results to return (default 5, max 10)"},
                "language": {"type": "string", "description": "Search language code (e.g. 'en', 'zh')"},
            },
            "required": ["query"],
        },
        "config": {"language": "en"},
        "config_schema": {
            "fields": [
                {
                    "key": "api_key",
                    "label": "API Key & Search Engine ID",
                    "type": "password",
                    "default": "",
                    "placeholder": "API_KEY:SEARCH_ENGINE_ID (get at console.cloud.google.com)",
                },
                {
                    "key": "language",
                    "label": "Search language",
                    "type": "select",
                    "options": [
                        {"value": "en", "label": "English"},
                        {"value": "zh-CN", "label": "Chinese"},
                        {"value": "ja", "label": "Japanese"},
                    ],
                    "default": "en",
                },
            ]
        },
    },
    {
        "name": "bing_search",
        "display_name": "Bing Search",
        "description": "Search using Bing Web Search API. Returns titles, URLs, and snippets. Requires a Bing Search API key from Microsoft Azure.",
        "category": "search",
        "icon": "🔍",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
                "max_results": {"type": "integer", "description": "Number of results to return (default 5, max 10)"},
                "language": {"type": "string", "description": "Market language code (e.g. 'en-US', 'zh-CN')"},
            },
            "required": ["query"],
        },
        "config": {"language": "en-US"},
        "config_schema": {
            "fields": [
                {
                    "key": "api_key",
                    "label": "Bing Search API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "Get from Azure Cognitive Services (Bing Search v7)",
                },
                {
                    "key": "language",
                    "label": "Market language",
                    "type": "select",
                    "options": [
                        {"value": "en-US", "label": "English (US)"},
                        {"value": "zh-CN", "label": "Chinese (Simplified)"},
                        {"value": "ja-JP", "label": "Japanese"},
                    ],
                    "default": "en-US",
                },
            ]
        },
    },
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
    {
        "name": "execute_code",
        "display_name": "Code Executor",
        "description": "Execute code (Python, Bash, Node.js) in a local sandboxed subprocess within the agent's workspace. Useful for data processing, calculations, file transformations, and automation.",
        "category": "code",
        "icon": "💻",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "enum": ["python", "bash", "node"],
                    "description": "Programming language",
                },
                "code": {"type": "string", "description": "Code to execute"},
                "timeout": {"type": "integer", "description": "Max execution time in seconds (default 30, max 60)"},
            },
            "required": ["language", "code"],
        },
        "config": {
            "sandbox_type": "subprocess",
            "cpu_limit": "0.5",
            "memory_limit": "256m",
            "allow_network": True,
            "default_timeout": 30,
            "max_timeout": 60,
        },
        "config_schema": {
            "fields": [
                {
                    "key": "cpu_limit",
                    "label": "CPU Limit",
                    "type": "text",
                    "default": "0.5",
                    "placeholder": "e.g., 0.5, 1.0, 2.0",
                },
                {
                    "key": "memory_limit",
                    "label": "Memory Limit",
                    "type": "text",
                    "default": "256m",
                    "placeholder": "e.g., 256m, 512m, 1g",
                },
                {
                    "key": "allow_network",
                    "label": "Allow Network Access",
                    "type": "checkbox",
                    "default": True,
                    "read_only_for_roles": ["agent_admin", "member"],
                },
                {
                    "key": "default_timeout",
                    "label": "Default Timeout (seconds)",
                    "type": "number",
                    "default": 30,
                    "min": 5,
                    "max": 3600,
                },
                {
                    "key": "max_timeout",
                    "label": "Max Timeout (seconds)",
                    "type": "number",
                    "default": 60,
                    "min": 10,
                    "max": 3600,
                },
            ]
        },
    },
    {
        "name": "execute_code_e2b",
        "display_name": "Code Executor (E2B Cloud)",
        "description": "Execute code (Python, Bash, Node.js) in a secure E2B cloud sandbox. Provides full network access and an isolated environment without consuming local resources. Requires an E2B API key.",
        "category": "code",
        "icon": "☁️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "enum": ["python", "bash", "node"],
                    "description": "Programming language",
                },
                "code": {"type": "string", "description": "Code to execute"},
                "timeout": {"type": "integer", "description": "Max execution time in seconds (default 30, max 60)"},
            },
            "required": ["language", "code"],
        },
        "config": {
            "sandbox_type": "e2b",
            "api_key": "",
            "default_timeout": 30,
            "max_timeout": 60,
        },
        "config_schema": {
            "fields": [
                {
                    "key": "api_key",
                    "label": "E2B API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "Get your API key at https://e2b.dev",
                    "required": True,
                },
                {
                    "key": "default_timeout",
                    "label": "Default Timeout (seconds)",
                    "type": "number",
                    "default": 30,
                    "min": 5,
                    "max": 3600,
                },
                {
                    "key": "max_timeout",
                    "label": "Max Timeout (seconds)",
                    "type": "number",
                    "default": 60,
                    "min": 10,
                    "max": 3600,
                },
            ]
        },
    },
    {
        "name": "upload_image",
        "display_name": "Upload Image",
        "description": "Upload images from the workspace or a URL to ImageKit CDN and get a public URL. Useful for sharing images externally or embedding them in reports.",
        "category": "code",
        "icon": "🖼️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Workspace-relative path to image file"},
                "url": {"type": "string", "description": "Public URL of image to upload"},
                "file_name": {"type": "string", "description": "Custom filename (optional)"},
                "folder": {"type": "string", "description": "CDN folder path (default /maraclaw)"},
            },
        },
        "config": {"private_key": "", "url_endpoint": ""},
        "config_schema": {
            "fields": [
                {
                    "key": "private_key",
                    "label": "ImageKit Private Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "Your ImageKit private API key",
                },
                {
                    "key": "url_endpoint",
                    "label": "ImageKit URL Endpoint",
                    "type": "text",
                    "default": "",
                    "placeholder": "https://ik.imagekit.io/your_imagekit_id",
                },
            ]
        },
    },
    {
        "name": "generate_image_siliconflow",
        "display_name": "Generate Image (SiliconFlow)",
        "description": "Generate an image via SiliconFlow FLUX models. China-friendly and fast.",
        "category": "media",
        "icon": "🎨",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed image description."},
                "size": {"type": "string", "description": "Image size (e.g. 1024x1024, 1024x768). Default 1024x1024."},
                "save_path": {"type": "string", "description": "Save path in workspace. Default: auto."},
            },
            "required": ["prompt"],
        },
        "config": {
            "model": "",
            "api_key": "",
            "base_url": "",
        },
        "config_schema": {
            "fields": [
                {
                    "key": "model",
                    "label": "Model",
                    "type": "text",
                    "default": "",
                    "placeholder": "e.g. black-forest-labs/FLUX.1-schnell",
                },
                {
                    "key": "api_key",
                    "label": "API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "SiliconFlow API Key",
                },
                {
                    "key": "base_url",
                    "label": "Base URL (optional)",
                    "type": "text",
                    "default": "",
                    "placeholder": "Default: https://api.siliconflow.cn/v1",
                },
            ]
        },
    },
    {
        "name": "generate_image_openai",
        "display_name": "Generate Image (OpenAI)",
        "description": "Generate an image via OpenAI DALL-E models.",
        "category": "media",
        "icon": "🎨",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed image description."},
                "size": {"type": "string", "description": "Image size (e.g. 1024x1024). Default 1024x1024."},
                "save_path": {"type": "string", "description": "Save path in workspace. Default: auto."},
            },
            "required": ["prompt"],
        },
        "config": {
            "model": "",
            "api_key": "",
            "base_url": "",
        },
        "config_schema": {
            "fields": [
                {
                    "key": "model",
                    "label": "Model",
                    "type": "text",
                    "default": "",
                    "placeholder": "e.g. dall-e-3 or dall-e-2",
                },
                {
                    "key": "api_key",
                    "label": "API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "OpenAI API Key",
                },
                {
                    "key": "base_url",
                    "label": "Base URL (optional)",
                    "type": "text",
                    "default": "",
                    "placeholder": "Default: https://api.openai.com/v1",
                },
            ]
        },
    },
    {
        "name": "generate_image_google",
        "display_name": "Generate Image (Google/Vertex)",
        "description": "Generate an image via Google Gemini Image (Nano Banana) or Vertex AI.",
        "category": "media",
        "icon": "🎨",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed image description."},
                "size": {"type": "string", "description": "Image size (e.g. 1024x1024). Default 1024x1024."},
                "save_path": {"type": "string", "description": "Save path in workspace. Default: auto."},
            },
            "required": ["prompt"],
        },
        "config": {
            "model": "",
            "api_key": "",
            "base_url": "",
        },
        "config_schema": {
            "fields": [
                {
                    "key": "model",
                    "label": "Model",
                    "type": "text",
                    "default": "",
                    "placeholder": "e.g. gemini-2.5-flash-image",
                },
                {
                    "key": "api_key",
                    "label": "API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "Google AI Studio or Vertex API Key",
                },
                {
                    "key": "base_url",
                    "label": "Base URL (optional)",
                    "type": "text",
                    "default": "",
                    "placeholder": "Can be Vertex API URL: https://aiplatform.googleapis.com/...",
                },
            ]
        },
    },
    {
        "name": "generate_image_custom",
        "display_name": "Generate Image (Custom API)",
        "description": "Generate an image through a custom OpenAI-compatible or gateway API. Configure the request body template and response image path for providers such as TokenRouter or OpenRouter.",
        "category": "media",
        "icon": "🎨",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed image description."},
                "size": {"type": "string", "description": "Image size (e.g. 1024x1024). Default 1024x1024."},
                "save_path": {"type": "string", "description": "Save path in workspace. Default: auto."},
            },
            "required": ["prompt"],
        },
        "config": {
            "api_key": "",
            "base_url": "",
            "endpoint_path": "/chat/completions",
            "model": "",
            "request_body_template_json": '{\n  "model": "{model}",\n  "messages": [\n    {\n      "role": "user",\n      "content": "{prompt}"\n    }\n  ],\n  "modalities": ["image", "text"],\n  "stream": false\n}',
            "response_image_path": "choices.0.message.images.0.image_url.url",
            "extra_headers_json": "",
            "timeout_seconds": 120,
        },
        "config_schema": {
            "fields": [
                {
                    "key": "api_key",
                    "label": "API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "API key for your image generation gateway",
                },
                {
                    "key": "model",
                    "label": "Model",
                    "type": "text",
                    "default": "",
                    "placeholder": "e.g. google/gemini-2.5-flash-image",
                },
                {
                    "key": "base_url",
                    "label": "Base URL",
                    "type": "text",
                    "default": "",
                    "placeholder": "e.g. https://api.tokenrouter.com/v1 or https://openrouter.ai/api/v1",
                },
                {
                    "key": "endpoint_path",
                    "label": "Endpoint Path",
                    "type": "text",
                    "default": "/chat/completions",
                    "placeholder": "/chat/completions",
                    "advanced": True,
                },
                {
                    "key": "request_body_template_json",
                    "label": "Request Body Template JSON",
                    "type": "textarea",
                    "default": '{\n  "model": "{model}",\n  "messages": [\n    {\n      "role": "user",\n      "content": "{prompt}"\n    }\n  ],\n  "modalities": ["image", "text"],\n  "stream": false\n}',
                    "placeholder": '{\n  "model": "{model}",\n  "messages": [{"role": "user", "content": "{prompt}"}],\n  "modalities": ["image", "text"],\n  "stream": false\n}',
                    "advanced": True,
                },
                {
                    "key": "response_image_path",
                    "label": "Response Image Path",
                    "type": "text",
                    "default": "choices.0.message.images.0.image_url.url",
                    "placeholder": "choices.0.message.images.0.image_url.url",
                    "advanced": True,
                },
                {
                    "key": "extra_headers_json",
                    "label": "Extra Headers JSON",
                    "type": "textarea",
                    "default": "",
                    "placeholder": '{\n  "HTTP-Referer": "https://your-app.example",\n  "X-Title": "MaraClaw"\n}',
                    "advanced": True,
                },
                {
                    "key": "timeout_seconds",
                    "label": "Timeout Seconds",
                    "type": "number",
                    "default": 120,
                    "min": 10,
                    "max": 600,
                    "advanced": True,
                },
            ]
        },
    },
    {
        "name": "discover_resources",
        "display_name": "Resource Discovery",
        "description": "Search public MCP registries (Smithery + ModelScope) for tools and capabilities that can extend your abilities. Use this when you encounter a task you cannot handle with your current tools.",
        "category": "discovery",
        "icon": "🔎",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Semantic description of the capability needed, e.g. 'send email', 'query SQL database', 'generate images'",
                },
                "max_results": {"type": "integer", "description": "Max results to return (default 5, max 10)"},
            },
            "required": ["query"],
        },
        "config": {},
        "config_schema": {
            "fields": [
                {
                    "key": "smithery_api_key",
                    "label": "Smithery API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "Get your key at smithery.ai/account/api-keys",
                },
                {
                    "key": "modelscope_api_token",
                    "label": "ModelScope API Token",
                    "type": "password",
                    "default": "",
                    "placeholder": "Get your token at modelscope.cn → Home → Access Tokens",
                },
            ]
        },
    },
    {
        "name": "import_mcp_server",
        "display_name": "Import MCP Server",
        "description": "Import an MCP server from Smithery registry into the platform. The server's tools become available for use. Use discover_resources first to find the server ID.",
        "category": "discovery",
        "icon": "📥",
        "is_default": True,
        "parameters_schema": {
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
            },
            "required": ["server_id"],
        },
        "config": {},
        "config_schema": {
            "fields": [
                {
                    "key": "smithery_api_key",
                    "label": "Smithery API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "Get your key at smithery.ai/account/api-keys",
                },
                {
                    "key": "modelscope_api_token",
                    "label": "ModelScope API Token",
                    "type": "password",
                    "default": "",
                    "placeholder": "Get your token at modelscope.cn → Home → Access Tokens",
                },
            ]
        },
    },
    # --- Email tools ---
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
    # --- OKR Tools ---
    # These tools expose the OKR system to agents. Not default - assigned explicitly
    # to the OKR Agent and to other agents that want to self-report progress.
    {
        "name": "get_okr",
        "display_name": "Get OKR Board",
        "description": (
            "Get the full OKR board for the current period. Returns all Objectives and Key Results "
            "for the tenant, organized by company and member level. Includes objective_id values "
            "for every Objective and kr_id values for every Key Result, so you can update existing "
            "Objectives and KRs instead of creating duplicates. Used by the OKR Agent to generate "
            "progress reports and monitor team performance."
        ),
        "category": "okr",
        "icon": "🎯",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "period_start": {
                    "type": "string",
                    "description": "Optional: ISO date string (YYYY-MM-DD) to filter by period start. Defaults to current period.",
                },
                "period_end": {
                    "type": "string",
                    "description": "Optional: ISO date string (YYYY-MM-DD) to filter by period end.",
                },
            },
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "get_my_okr",
        "display_name": "My OKR",
        "description": (
            "Get your own OKR Objectives and Key Results for the current period. "
            "Returns a structured view of your goals, current progress values, plus objective_id and kr_id references "
            "you need to update existing OKRs correctly. Call this before changing progress, KR content, "
            "or Objective text so you reuse the current records instead of creating duplicates."
        ),
        "category": "okr",
        "icon": "🎯",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "period_start": {
                    "type": "string",
                    "description": "Optional: ISO date string (YYYY-MM-DD). Defaults to current period.",
                },
                "period_end": {
                    "type": "string",
                    "description": "Optional: ISO date string (YYYY-MM-DD).",
                },
            },
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "update_kr_progress",
        "display_name": "Update KR Progress",
        "description": (
            "Update the current progress value for a Key Result. Use get_my_okr first to obtain "
            "the kr_id. The status (on_track / at_risk / behind / completed) is automatically "
            "computed from the progress ratio, or you can override it explicitly. "
            "A progress log entry is recorded for full audit history."
        ),
        "category": "okr",
        "icon": "📈",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "kr_id": {
                    "type": "string",
                    "description": "UUID of the Key Result to update. Get this from get_my_okr.",
                },
                "value": {
                    "type": "number",
                    "description": "New current value (e.g. 4.2 for a KR with target 5.0).",
                },
                "note": {
                    "type": "string",
                    "description": "Optional note explaining the progress update (e.g. 'Completed weekly review session').",
                },
                "status": {
                    "type": "string",
                    "enum": ["on_track", "at_risk", "behind", "completed"],
                    "description": "Optional: override the auto-computed status.",
                },
            },
            "required": ["kr_id", "value"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "update_kr_content",
        "display_name": "Update KR Content",
        "description": (
            "Update the content fields of one of YOUR OWN Key Results, such as title, target value, unit, "
            "focus reference, or status. Use get_my_okr first to obtain the kr_id. "
            "This tool is for changing KR definition/content, not reporting progress. "
            "If the user says to change, revise, adjust, or replace an existing KR target or wording, "
            "prefer this tool instead of create_key_result."
        ),
        "category": "okr",
        "icon": "✏️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "kr_id": {
                    "type": "string",
                    "description": "UUID of the Key Result to update (from get_my_okr).",
                },
                "title": {
                    "type": "string",
                    "description": "Optional new KR title.",
                },
                "target_value": {
                    "type": "number",
                    "description": "Optional new target value.",
                },
                "unit": {
                    "type": "string",
                    "description": "Optional new unit label.",
                },
                "focus_ref": {
                    "type": "string",
                    "description": "Optional new focus file reference.",
                },
                "status": {
                    "type": "string",
                    "enum": ["on_track", "at_risk", "behind", "completed"],
                    "description": "Optional explicit status override.",
                },
            },
            "required": ["kr_id"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        # collect_okr_progress - legacy OKR Agent heartbeat collection path.
        # This replaces the need to contact each member individually.
        "name": "collect_okr_progress",
        "display_name": "Collect OKR Progress",
        "description": (
            "Legacy batch sync for reported KR progress. Prefer direct OKR tools such as "
            "get_my_okr and update_kr_progress for new work. Returns a summary of how many "
            "KRs were updated."
        ),
        "category": "okr",
        "icon": "📊",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "config": {"okr_agent_only": True},
        "config_schema": {},
    },
    {
        # generate_okr_report - OKR Agent calls this to produce the structured report.
        # The tool writes the report to WorkReport table and returns the markdown content
        # so the Agent can choose to post it to Plaza or send it to specific channels.
        "name": "generate_okr_report",
        "display_name": "Generate OKR Report",
        "description": (
            "Generate a structured OKR progress report (daily or weekly) for the current "
            "period. The report summarizes all Objectives and Key Results, highlights items "
            "at risk or behind, and shows overall team health metrics. The report is saved "
            "to the database and to your workspace/reports/ folder. Returns the full report "
            "markdown so you can post it to Plaza or share with the team."
        ),
        "category": "okr",
        "icon": "📋",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "report_type": {
                    "type": "string",
                    "enum": ["daily", "weekly"],
                    "description": "Whether to generate a daily or weekly report.",
                },
            },
            "required": ["report_type"],
        },
        "config": {"okr_agent_only": True},
        "config_schema": {},
    },
    {
        # get_okr_settings - lets OKR Agent read the tenant's OKR configuration so it
        # can determine whether reports are due, what time they're scheduled, etc.
        "name": "get_okr_settings",
        "display_name": "Get OKR Settings",
        "description": (
            "Read the OKR configuration for this team, including whether daily/weekly "
            "reports are enabled, the configured report time, period frequency, and more. "
            "Use this at the start of your heartbeat to decide whether a report is due today."
        ),
        "category": "okr",
        "icon": "⚙️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "config": {"okr_agent_only": True},
        "config_schema": {},
    },
    {
        # create_objective - OKR Agent uses this after conversation-based confirmation
        # to create an O for the company, a user, or an agent. Only OKR Agent has this tool.
        "name": "create_objective",
        "display_name": "Create Objective",
        "description": (
            "Create an OKR Objective for the company, a specific user, or a specific agent. "
            "Call this after confirming the objective with the relevant person through conversation. "
            "Use this only when a new Objective needs to be created for the period. "
            "If the person already has a matching Objective and just wants to revise it, use update_objective instead. "
            "owner_type must be 'company', 'user', or 'agent'. "
            "owner_id is not required for company-level objectives. "
            "period_start and period_end must be ISO date strings (YYYY-MM-DD)."
        ),
        "category": "okr",
        "icon": "🎯",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "The objective title (concise, inspiring, directional).",
                },
                "description": {
                    "type": "string",
                    "description": "Optional detailed description of the objective.",
                },
                "owner_type": {
                    "type": "string",
                    "enum": ["company", "user", "agent"],
                    "description": "Who this objective belongs to.",
                },
                "owner_id": {
                    "type": "string",
                    "description": "UUID of the owner. Try to use this if available in context.",
                },
                "owner_name": {
                    "type": "string",
                    "description": "Optional fallback: the exact display name of the human/agent. Use this ONLY if you don't have their UUID.",
                },
                "period_start": {
                    "type": "string",
                    "description": "ISO date string for the start of the OKR period (e.g. '2026-04-01').",
                },
                "period_end": {
                    "type": "string",
                    "description": "ISO date string for the end of the OKR period (e.g. '2026-06-30').",
                },
            },
            "required": ["title", "owner_type", "period_start", "period_end"],
        },
        "config": {"okr_agent_only": True},
        "config_schema": {},
    },
    {
        # create_key_result - OKR Agent creates a measurable KR under a confirmed objective.
        "name": "create_key_result",
        "display_name": "Create Key Result",
        "description": (
            "Create a Key Result (KR) under an existing Objective. "
            "Get the objective_id first using get_okr. "
            "Use this only for a brand-new KR. If the user is revising the wording, target value, unit, "
            "or focus reference of an existing KR, use update_kr_content instead. "
            "target_value is the goal number (e.g. 50000 for 50000 followers). "
            "unit is optional but recommended for clarity (e.g. '%', 'NPS', '万元', 'followers')."
        ),
        "category": "okr",
        "icon": "🔑",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "objective_id": {
                    "type": "string",
                    "description": "UUID of the parent Objective.",
                },
                "title": {
                    "type": "string",
                    "description": "The KR title (specific, measurable outcome).",
                },
                "target_value": {
                    "type": "number",
                    "description": "The target number to achieve (e.g. 50000).",
                },
                "unit": {
                    "type": "string",
                    "description": "Optional unit label (e.g. '%', 'followers', '万元', 'NPS score').",
                },
                "focus_ref": {
                    "type": "string",
                    "description": "Optional: basename of the focus file that tracks this KR (e.g. 'content_quality').",
                },
            },
            "required": ["objective_id", "title", "target_value"],
        },
        "config": {"okr_agent_only": True},
        "config_schema": {},
    },
    {
        # update_objective - available to ALL agents, but with ownership enforcement:
        # regular agents can only modify their own O; OKR Agent can modify any O.
        "name": "update_objective",
        "display_name": "Update Objective",
        "description": (
            "Modify an Objective's title, description, status, or period dates. "
            "Regular agents can only update their own Objectives - call get_my_okr first "
            "to get your objective_id. The OKR Agent can update any member's Objective. "
            "Only provide the fields you want to change. If the request is to revise an existing OKR's "
            "goal text rather than create a new one, prefer this tool over create_objective."
        ),
        "category": "okr",
        "icon": "✏️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "objective_id": {
                    "type": "string",
                    "description": "UUID of the Objective to update. Get from get_my_okr (own) or get_okr (any).",
                },
                "title": {
                    "type": "string",
                    "description": "New title for the objective.",
                },
                "description": {
                    "type": "string",
                    "description": "New description.",
                },
                "status": {
                    "type": "string",
                    "enum": ["draft", "active", "completed", "archived"],
                    "description": "New status for the objective.",
                },
                "period_start": {
                    "type": "string",
                    "description": "New period start date (YYYY-MM-DD).",
                },
                "period_end": {
                    "type": "string",
                    "description": "New period end date (YYYY-MM-DD).",
                },
            },
            "required": ["objective_id"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        # update_any_kr_progress - OKR Agent exclusive: update KR for any member.
        # Unlike update_kr_progress (self-report), this can update anyone's KR.
        # Used after collecting progress data through conversation.
        "name": "update_any_kr_progress",
        "display_name": "Update Any KR Progress",
        "description": (
            "Update the progress value of any team member's Key Result. "
            "This is the OKR Agent's exclusive version of update_kr_progress - it can update "
            "KRs belonging to any user or agent, not just the caller's own. "
            "Use this ONLY after confirming the value with the KR owner through conversation. "
            "Get kr_id from get_okr. Optionally provide a note explaining the source."
        ),
        "category": "okr",
        "icon": "📈",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "kr_id": {
                    "type": "string",
                    "description": "UUID of the Key Result to update. Get from get_okr.",
                },
                "value": {
                    "type": "number",
                    "description": "New current value for this KR.",
                },
                "note": {
                    "type": "string",
                    "description": "Source or context note (e.g. 'Reported by user in weekly check-in').",
                },
                "status": {
                    "type": "string",
                    "enum": ["on_track", "at_risk", "behind", "completed"],
                    "description": "Optional: override the auto-computed status.",
                },
            },
            "required": ["kr_id", "value"],
        },
        "config": {"okr_agent_only": True},
        "config_schema": {},
    },
    {
        # generate_monthly_okr_report - OKR Agent exclusive: produce the monthly summary report.
        # Called automatically by the monthly_okr_report system cron trigger, or on-demand.
        "name": "generate_monthly_okr_report",
        "display_name": "Generate Monthly OKR Report",
        "description": (
            "Generate the monthly OKR progress summary report. Covers all Objectives and Key "
            "Results for the current period, highlights completed and at-risk items, and provides "
            "a closing action note. Saved to WorkReport (report_type='monthly') and "
            "workspace/reports/. Returns the full Markdown so you can send it to admins."
        ),
        "category": "okr",
        "icon": "📅",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "config": {"okr_agent_only": True},
        "config_schema": {},
    },
    {
        # upsert_member_daily_report - OKR Agent exclusive: create or revise a member daily report.
        "name": "upsert_member_daily_report",
        "display_name": "Upsert Member Daily Report",
        "description": (
            "Create or update the final normalized daily report for any member in the company. "
            "Use this after discussing progress with the member and distilling their update into "
            "one concise final report. The stored content should stay within 2000 characters."
        ),
        "category": "okr",
        "icon": "📝",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "report_date": {
                    "type": "string",
                    "description": "Report date in YYYY-MM-DD format.",
                },
                "content": {
                    "type": "string",
                    "description": "Final concise daily report content. Keep it within 2000 characters.",
                },
                "member_type": {
                    "type": "string",
                    "enum": ["user", "agent"],
                    "description": "Member type. Defaults to user if omitted.",
                },
                "member_id": {
                    "type": "string",
                    "description": "UUID of the member. Preferred when available.",
                },
                "member_name": {
                    "type": "string",
                    "description": "Member display name. Use when you do not have the UUID.",
                },
                "source": {
                    "type": "string",
                    "description": "Optional source tag such as okr_agent_assisted or manual.",
                },
            },
            "required": ["report_date", "content"],
        },
        "config": {"okr_agent_only": True},
        "config_schema": {},
    },
    # --- Feishu Integration Tools ---
    # These tools require a configured Feishu channel to function.
    # They are NOT enabled by default - agents with Feishu channels should enable them.
    {
        "name": "send_feishu_message",
        "display_name": "Feishu Message",
        "description": "Send a message to a human colleague via Feishu. Can only message people in your relationships.",
        "category": "feishu",
        "icon": "💬",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "member_name": {"type": "string", "description": "Recipient name"},
                "message": {"type": "string", "description": "Message content"},
            },
            "required": ["member_name", "message"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_user_search",
        "display_name": "Feishu User Search",
        "description": "Search for a colleague in the Feishu (Lark) directory by name. Returns their open_id, email, and department.",
        "category": "feishu",
        "icon": "🔍",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The colleague's name to search for"},
            },
            "required": ["name"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "bitable_create_app",
        "display_name": "Bitable Create",
        "description": "Create a Feishu Bitable app in Feishu Drive. Returns a direct link and App Token. Next, use bitable_list_tables to view the initial tables.",
        "category": "feishu",
        "icon": "📊",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the new Bitable, for example 「项目追踪表」."},
                "folder_token": {
                    "type": "string",
                    "description": "Optional parent folder_token. Defaults to the root of 「我的空间」.",
                },
            },
            "required": ["name"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "bitable_list_tables",
        "display_name": "Bitable List Tables",
        "description": "List all tables in a Feishu Bitable. url supports Bitable and Wiki links. Use this tool to identify the tables in the requested Bitable.",
        "category": "feishu",
        "icon": "📊",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The Bitable URL."},
            },
            "required": ["url"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "bitable_list_fields",
        "display_name": "Bitable List Fields",
        "description": "List all fields in a specified Feishu Bitable table. url supports Bitable and Wiki links. You must call this tool before querying or modifying data to learn the field names and types.",
        "category": "feishu",
        "icon": "⌨️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The Bitable URL."},
                "table_id": {"type": "string", "description": "The specific table ID. Optional when url includes tbl."},
            },
            "required": ["url"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "bitable_query_records",
        "display_name": "Bitable Query Records",
        "description": "Query rows in a Feishu Bitable. An optional filter can be provided.",
        "category": "feishu",
        "icon": "🔍",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The Bitable URL."},
                "table_id": {"type": "string", "description": "The specific table ID. Optional when url includes tbl."},
                "filter_info": {
                    "type": "string",
                    "description": "Optional FQL filter, for example 'CurrentValue.[Status]=\"Done\"'. If you are unsure of the filter syntax, omit this and filter all returned data locally.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of records to return (default: 100).",
                },
            },
            "required": ["url"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "bitable_create_record",
        "display_name": "Bitable Create Record",
        "description": "Create a row in a Feishu Bitable. fields is a dictionary whose keys are field names (obtain them with bitable_list_fields first) and values are the corresponding values.",
        "category": "feishu",
        "icon": "➕",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The Bitable URL."},
                "table_id": {"type": "string", "description": "The specific table ID. Optional when url includes tbl."},
                "fields": {
                    "type": "string",
                    "description": 'A JSON string representing the fields to insert. Example: \'{"Name": "张三", "Age": 30}\'',
                },
            },
            "required": ["url", "fields"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "bitable_update_record",
        "display_name": "Bitable Update Record",
        "description": "Update a specified row in a Feishu Bitable.",
        "category": "feishu",
        "icon": "✏️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The Bitable URL."},
                "table_id": {"type": "string", "description": "The specific table ID. Optional when url includes tbl."},
                "record_id": {
                    "type": "string",
                    "description": "The record_id to update, obtained with bitable_query_records.",
                },
                "fields": {
                    "type": "string",
                    "description": 'A JSON string representing the fields to update. Example: \'{"Status": "Done"}\'',
                },
            },
            "required": ["url", "record_id", "fields"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "bitable_delete_record",
        "display_name": "Bitable Delete Record",
        "description": "Delete a specified row from a Feishu Bitable.",
        "category": "feishu",
        "icon": "🗑️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The Bitable URL."},
                "table_id": {"type": "string", "description": "The specific table ID. Optional when url includes tbl."},
                "record_id": {
                    "type": "string",
                    "description": "The record_id to delete, obtained with bitable_query_records.",
                },
            },
            "required": ["url", "record_id"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_doc_search",
        "display_name": "Feishu Doc Search",
        "description": "Search Feishu cloud documents by keyword using the official document search API. Useful when a wiki or knowledge base has too many files to browse manually.",
        "category": "feishu",
        "icon": "🔎",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keyword, e.g. '恩菲' or '客户周报'"},
                "docs_types": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["doc", "docx", "sheet", "bitable", "file", "folder", "mindnote", "slides"],
                    },
                    "description": "Optional file type filter.",
                },
                "count": {"type": "integer", "description": "Number of results to return (default 10, max 50)."},
                "offset": {"type": "integer", "description": "Result offset for pagination (default 0)."},
            },
            "required": ["query"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_doc_read",
        "display_name": "Feishu Doc Read",
        "description": "Read the text content of a Feishu document (Docx). Provide the document token from its URL.",
        "category": "feishu",
        "icon": "📄",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "document_token": {"type": "string", "description": "Feishu document token (from document URL)"},
                "max_chars": {"type": "integer", "description": "Max characters to return (default 6000, max 20000)"},
            },
            "required": ["document_token"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_doc_create",
        "display_name": "Feishu Doc Create",
        "description": "Create a new Feishu document with a given title. Returns the new document token and URL.",
        "category": "feishu",
        "icon": "📝",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Document title"},
                "folder_token": {"type": "string", "description": "Optional: parent folder token"},
            },
            "required": ["title"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_doc_append",
        "display_name": "Feishu Doc Append",
        "description": "Append text content to an existing Feishu document as new paragraphs at the end.",
        "category": "feishu",
        "icon": "📎",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "document_token": {"type": "string", "description": "Feishu document token"},
                "content": {"type": "string", "description": "Text content to append"},
            },
            "required": ["document_token", "content"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_drive_share",
        "display_name": "Feishu Drive Share",
        "description": "Manage collaborators for any Feishu Drive file (docx, bitable, sheet, etc.). Add, remove, or list collaborators with view/edit/full_access permissions.",
        "category": "feishu",
        "icon": "🔗",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "document_token": {"type": "string", "description": "File token (from URL or previous tool output)"},
                "doc_type": {
                    "type": "string",
                    "enum": ["docx", "bitable", "sheet", "doc", "folder", "mindnote", "slides"],
                    "description": "File type. Default: 'docx'",
                },
                "action": {
                    "type": "string",
                    "enum": ["add", "remove", "list"],
                    "description": "'add' to grant, 'remove' to revoke, 'list' to view",
                },
                "member_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Colleague names to add/remove (auto-searched)",
                },
                "member_open_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Feishu open_ids directly",
                },
                "permission": {
                    "type": "string",
                    "enum": ["view", "edit", "full_access"],
                    "description": "Permission level. Default: 'edit'",
                },
            },
            "required": ["document_token", "action"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_drive_delete",
        "display_name": "Feishu Drive Delete",
        "description": "Delete a file or folder from Feishu Drive. The file is moved to the recycle bin. Supports all file types: docx, bitable, sheet, folder, etc.",
        "category": "feishu",
        "icon": "🗑️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_token": {"type": "string", "description": "Token of the file to delete"},
                "file_type": {
                    "type": "string",
                    "enum": ["file", "docx", "bitable", "folder", "doc", "sheet", "mindnote", "shortcut", "slides"],
                    "description": "Type of the file to delete",
                },
            },
            "required": ["file_token", "file_type"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_calendar_list",
        "display_name": "Feishu Calendar List",
        "description": "List Feishu calendar events. No email or authorization needed.",
        "category": "feishu",
        "icon": "📅",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "start_time": {"type": "string", "description": "Range start, ISO 8601. Default: now."},
                "end_time": {"type": "string", "description": "Range end, ISO 8601. Default: 7 days from now."},
                "max_results": {"type": "integer", "description": "Max events to return (default 20)"},
            },
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_calendar_create",
        "display_name": "Feishu Calendar Create",
        "description": "Create a Feishu calendar event. Supports inviting colleagues by name. No email needed.",
        "category": "feishu",
        "icon": "📅",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title"},
                "start_time": {"type": "string", "description": "Event start in ISO 8601 with timezone"},
                "end_time": {"type": "string", "description": "Event end in ISO 8601 with timezone"},
                "description": {"type": "string", "description": "Event description or agenda"},
                "attendee_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names of colleagues to invite",
                },
                "location": {"type": "string", "description": "Event location"},
            },
            "required": ["summary", "start_time", "end_time"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_calendar_update",
        "display_name": "Feishu Calendar Update",
        "description": "Update an existing Feishu calendar event. Provide only the fields you want to change.",
        "category": "feishu",
        "icon": "📅",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "user_email": {"type": "string", "description": "Calendar owner's email"},
                "event_id": {"type": "string", "description": "Event ID from feishu_calendar_list"},
                "summary": {"type": "string", "description": "New title"},
                "start_time": {"type": "string", "description": "New start time (ISO 8601)"},
                "end_time": {"type": "string", "description": "New end time (ISO 8601)"},
            },
            "required": ["user_email", "event_id"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_calendar_delete",
        "display_name": "Feishu Calendar Delete",
        "description": "Delete (cancel) a Feishu calendar event.",
        "category": "feishu",
        "icon": "🗑️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "user_email": {"type": "string", "description": "Calendar owner's email"},
                "event_id": {"type": "string", "description": "Event ID to delete"},
            },
            "required": ["user_email", "event_id"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_approval_create",
        "display_name": "Feishu Approval Create",
        "description": "Start a Feishu approval workflow instance. You must know the approval definition's approval_code and the content for its form fields.",
        "category": "feishu",
        "icon": "📝",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "approval_code": {
                    "type": "string",
                    "description": "The approval definition's unique code (approval_code).",
                },
                "user_id": {
                    "type": "string",
                    "description": "The initiator's open_id. Obtain it with feishu_user_search.",
                },
                "form_data": {
                    "type": "string",
                    "description": 'A JSON string containing the form data, for example \'[{"id":"widget1","type":"input","value":"这是内容"}]\'',
                },
            },
            "required": ["approval_code", "user_id", "form_data"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_approval_query",
        "display_name": "Feishu Approval Query",
        "description": "Query a list of specified Feishu approval instances. Supports filtering by status (PENDING, APPROVED, REJECTED, CANCELED, DELETED).",
        "category": "feishu",
        "icon": "📋",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "approval_code": {
                    "type": "string",
                    "description": "The approval definition's unique code (approval_code).",
                },
                "status": {
                    "type": "string",
                    "description": "Optional status filter: PENDING, APPROVED, REJECTED, CANCELED, DELETED.",
                },
            },
            "required": ["approval_code"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_approval_get",
        "display_name": "Feishu Approval Get",
        "description": "Get detailed information and the current approval status for a specified Feishu approval instance.",
        "category": "feishu",
        "icon": "📊",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "The approval instance's instance_id."},
            },
            "required": ["instance_id"],
        },
        "config": {},
        "config_schema": {},
    },
    # --- Pages: public HTML hosting ---
    {
        "name": "publish_page",
        "display_name": "Publish Page",
        "description": "Publish an HTML file from workspace as a public page. Returns a public URL that anyone can access without login. Only .html/.htm files can be published.",
        "category": "pages",
        "icon": "🌐",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path in workspace, e.g. 'workspace/output.html'"},
            },
            "required": ["path"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "list_published_pages",
        "display_name": "List Published Pages",
        "description": "List all pages published by this agent, showing their public URLs and view counts.",
        "category": "pages",
        "icon": "📋",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {},
        },
        "config": {},
        "config_schema": {},
    },
    # --- Skill Management ---
    {
        "name": "search_clawhub",
        "display_name": "Search ClawHub",
        "description": "Search the ClawHub skill registry for skills matching a query. Returns a list of available skills with name, description, and last updated date.",
        "category": "discovery",
        "icon": "🔎",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query, e.g. 'research', 'code review', 'market analysis'",
                },
            },
            "required": ["query"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "install_skill",
        "display_name": "Install Skill",
        "description": "Install a skill into this agent's workspace. Accepts a ClawHub slug (e.g. 'market-research') or a GitHub URL.",
        "category": "discovery",
        "icon": "📥",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "ClawHub skill slug (e.g. 'market-research') or GitHub URL",
                },
            },
            "required": ["source"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "update_kr_content",
        "display_name": "Update KR Content",
        "description": (
            "Update the content fields of one of YOUR OWN Key Results. "
            "Call get_my_okr first to obtain the kr_id, then change title, target_value, unit, "
            "focus_ref, or status as needed. This does not record a progress update."
        ),
        "category": "okr",
        "icon": "✏️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "kr_id": {
                    "type": "string",
                    "description": "UUID of the Key Result to update (from get_my_okr).",
                },
                "title": {
                    "type": "string",
                    "description": "Optional new KR title.",
                },
                "target_value": {
                    "type": "number",
                    "description": "Optional new target value.",
                },
                "unit": {
                    "type": "string",
                    "description": "Optional new unit label.",
                },
                "focus_ref": {
                    "type": "string",
                    "description": "Optional new focus reference.",
                },
                "status": {
                    "type": "string",
                    "enum": ["on_track", "at_risk", "behind", "completed"],
                    "description": "Optional explicit status value.",
                },
            },
            "required": ["kr_id"],
        },
        "config": {},
        "config_schema": {},
    },
]

BUILTIN_TOOLS = [
    *BUILTIN_TOOLS,
    # ── AgentBay Tools ──
    *AGENTBAY_TOOLS,
]

BUILTIN_TOOLS = [
    *BUILTIN_TOOLS,
    *OKR_BUILTIN_TOOLS,
    *DEPLOY_BUILTIN_TOOLS,
]
