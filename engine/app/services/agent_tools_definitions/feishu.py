"""OpenAI function-calling catalog slice. Data only."""

FEISHU_AGENT_TOOLS: list[object] = [
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
]
