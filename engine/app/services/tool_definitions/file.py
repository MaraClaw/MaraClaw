"""Seed rows extracted from builtin.py. Data only."""


from typing import Any

FILE_TOOLS: list[dict[str, Any]] = [
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
]
