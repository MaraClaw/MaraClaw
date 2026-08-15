"""OpenAI function-calling catalog slice. Data only."""

CORE_AGENT_TOOLS: list[object] = [
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
]
