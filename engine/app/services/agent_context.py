"""Build rich system prompt context for agents.

Loads soul, memory, skills summary, and relationships from the agent's
workspace files and composes a comprehensive system prompt.
"""

import uuid
from pathlib import Path

from app.config import get_settings
from app.core.logging import logger
from app.services.storage import get_storage_backend, normalize_storage_key

settings = get_settings()


async def _read_file_safe(key: str, max_chars: int = 3000) -> str:
    """Read a storage-backed text file, return empty string if missing."""
    storage = get_storage_backend()
    if not await storage.exists(key) or not await storage.is_file(key):
        return ""
    try:
        content = (await storage.read_text(key, encoding="utf-8", errors="replace")).strip()
        if len(content) > max_chars:
            content = content[:max_chars] + "\n...(truncated)"
        return content
    except Exception:
        return ""


def _parse_skill_frontmatter(content: str, filename: str) -> tuple[str, str]:
    """Parse YAML frontmatter from a skill .md file.

    Returns (name, description).
    If no frontmatter, falls back to filename-based name and first-line description.
    """
    name = filename.replace("_", " ").replace("-", " ")
    description = ""

    stripped = content.strip()
    if stripped.startswith("---"):
        end = stripped.find("---", 3)
        if end != -1:
            frontmatter = stripped[3:end].strip()
            for line in frontmatter.split("\n"):
                line = line.strip()
                if line.lower().startswith("name:"):
                    val = line[5:].strip().strip('"').strip("'")
                    if val:
                        name = val
                elif line.lower().startswith("description:"):
                    val = line[12:].strip().strip('"').strip("'")
                    if val:
                        description = val[:200]
            if description:
                return name, description

    # Fallback: use first non-empty, non-heading line as description
    for line in stripped.split("\n"):
        line = line.strip()
        # Skip frontmatter delimiters and YAML lines
        if line == "---" or line.startswith(("name:", "description:")):
            continue
        if line and not line.startswith("#"):
            description = line[:200]
            break
    if not description:
        lines = stripped.split("\n")
        if lines:
            description = lines[0].strip().lstrip("# ")[:200]

    return name, description


async def _load_skills_index(agent_id: uuid.UUID) -> str:
    """Load skill index (name + description) from skills/ directory.

    Supports two formats:
    - Flat file:   skills/my-skill.md
    - Folder:      skills/my-skill/SKILL.md  (Claude-style, with optional scripts/, references/)

    Uses progressive disclosure: only name+description go into the system
    prompt. The model is instructed to call read_file to load full content
    when a skill is relevant.
    """
    skills: list[tuple[str, str, str]] = []  # (name, description, path_relative_to_skills)
    storage = get_storage_backend()
    skills_prefix = normalize_storage_key(f"{agent_id}/skills")
    if await storage.exists(skills_prefix) and await storage.is_dir(skills_prefix):
        for entry in await storage.list_dir(skills_prefix):
            if entry.name.startswith("."):
                continue
            entry_key = entry.key

            # Case 1: Folder-based skill — skills/<folder>/SKILL.md
            if entry.is_dir:
                skill_md_key = f"{entry_key}/SKILL.md"
                if not await storage.exists(skill_md_key):
                    skill_md_key = f"{entry_key}/skill.md"
                if await storage.exists(skill_md_key):
                    try:
                        content = (await storage.read_text(skill_md_key, encoding="utf-8", errors="replace")).strip()
                        name, desc = _parse_skill_frontmatter(content, entry.name)
                        skills.append((name, desc, f"{entry.name}/SKILL.md"))
                    except Exception:
                        skills.append((entry.name, "", f"{entry.name}/SKILL.md"))

            # Case 2: Flat file — skills/<name>.md
            elif Path(entry.name).suffix == ".md" and not entry.is_dir:
                try:
                    content = (await storage.read_text(entry_key, encoding="utf-8", errors="replace")).strip()
                    name, desc = _parse_skill_frontmatter(content, Path(entry.name).stem)
                    skills.append((name, desc, entry.name))
                except Exception:
                    skills.append((Path(entry.name).stem, "", entry.name))

    # Deduplicate by name
    seen: set[str] = set()
    unique: list[tuple[str, str, str]] = []
    for s in skills:
        if s[0] not in seen:
            seen.add(s[0])
            unique.append(s)

    if not unique:
        return ""

    # Build index table
    lines = [
        "You have the following skills available. Each skill defines specific instructions for a task domain.",
        "",
        "| Skill | Description | File |",
        "|-------|-------------|------|",
    ]
    for name, desc, rel_path in unique:
        lines.append(f"| {name} | {desc} | skills/{rel_path} |")

    lines.append("")
    lines.append("⚠️ SKILL USAGE RULES:")
    lines.append(
        "1. When a user request matches a skill, FIRST call `read_file` with the File path above to load the full instructions."
    )
    lines.append("2. Follow the loaded instructions to complete the task.")
    lines.append("3. Do NOT guess what the skill contains — always read it first.")
    lines.append(
        "4. Folder-based skills may contain auxiliary files (scripts/, references/, examples/). Use `list_files` on the skill folder to discover them."
    )

    return "\n".join(lines)


async def _load_relationships_from_db(agent_id: uuid.UUID) -> str:
    """Query relationships directly from the database and format as a markdown list."""
    from app.core.permissions import evaluate_agent_relationship_status, evaluate_human_relationship_status
    from app.dao.agent_agent_relationship_dao import agent_agent_relationship_dao
    from app.dao.agent_relationship_dao import agent_relationship_dao

    def _display_provider_name(pn, pt):
        if not pn and not pt:
            return None
        if (pt or "").lower() in ("web", "platform") or (pn or "").lower() == "web":
            return "Platform"
        return pn

    human_rows = []
    for rel in await agent_relationship_dao.list_for_agent_with_members_and_providers(agent_id):
        status_info = await evaluate_human_relationship_status(None, rel)
        if status_info["access_status"] == "active":
            human_rows.append((rel, _display_provider_name(rel.provider_name, rel.provider_type)))

    agent_rels = []
    for rel in await agent_agent_relationship_dao.list_for_agent_with_targets(agent_id):
        status_info = await evaluate_agent_relationship_status(None, rel)
        if status_info["access_status"] == "active":
            agent_rels.append(rel)

    if not human_rows and not agent_rels:
        return ""

    lines = []

    if human_rows:
        lines.append("## Human Colleagues\n")
        for r, provider_name in human_rows:
            m = r.member
            if not m:
                continue
            source = f" (synced through {provider_name})" if provider_name else ""
            lines.append(f"### {m.name} — {m.title or 'Position not set'}{source}")
            if r.description:
                lines.append(f"- {r.description}")
            lines.append("")

    if agent_rels:
        lines.append("## 🤖 Digital Employee Colleagues\n")
        for r in agent_rels:
            a = r.target_agent
            if not a:
                continue
            lines.append(f"### {a.name} — {a.role_description or 'Digital employee'}")
            if r.description:
                lines.append(f"- {r.description}")
            lines.append("")

    return "\n".join(lines).strip()


async def build_agent_context(
    agent_id: uuid.UUID,
    agent_name: str,
    role_description: str = "",
    current_user_name: str | None = None,
) -> tuple[str, str]:
    """Build a rich system prompt incorporating agent's full context.

    Reads from workspace files and DB:
    - soul.md → personality
    - memory.md → long-term memory
    - skills/ → skill names + summaries
    - Database → relationship network (human + agent)
    """
    from app.services.agent_context_cache import get_cached_text, set_cached_text

    # --- Soul ---
    # Soul is the agent's full author-curated identity; detailed souls (e.g.
    # bundle agents) run 4-12k chars. A tight cap silently drops every tail
    # section — rules, boundaries, facts — and the agent then confidently
    # denies things its soul plainly states, with no log of the truncation.
    # Memory and relationships below keep small caps because they grow
    # unbounded at runtime; the soul does not (only seeded/explicitly edited).
    soul = await get_cached_text(agent_id, "soul")
    if soul is None:
        soul = await _read_file_safe(normalize_storage_key(f"{agent_id}/soul.md"), 30000)
        if soul.startswith("# "):
            soul = "\n".join(soul.split("\n")[1:]).strip()
        await set_cached_text(agent_id, "soul", soul)

    # --- Memory ---
    memory = await get_cached_text(agent_id, "memory")
    if memory is None:
        memory = await _read_file_safe(normalize_storage_key(f"{agent_id}/memory/memory.md"), 2000)
        if not memory:
            memory = await _read_file_safe(normalize_storage_key(f"{agent_id}/memory.md"), 2000)
        if memory.startswith("# "):
            memory = "\n".join(memory.split("\n")[1:]).strip()
        await set_cached_text(agent_id, "memory", memory)

    # --- Skills index (progressive disclosure) ---
    skills_text = await get_cached_text(agent_id, "skills")
    if skills_text is None:
        skills_text = await _load_skills_index(agent_id)
        await set_cached_text(agent_id, "skills", skills_text)

    # --- Relationships ---
    relationships = await _load_relationships_from_db(agent_id)

    # --- Compose static and dynamic system prompt blocks ---
    from app.services.timezone_utils import get_agent_timezone, now_in_timezone

    agent_tz_name = await get_agent_timezone(agent_id)
    agent_local_now = now_in_timezone(agent_tz_name)
    now_str = agent_local_now.strftime(f"%Y-%m-%d %H:%M:%S ({agent_tz_name})")

    static_parts = [f"You are {agent_name}, an enterprise digital employee."]

    if role_description:
        static_parts.append(f"\n## Role\n{role_description}")

    if agent_name == "OKR Agent":
        static_parts.append("""
## Daily Report Recording Rules

🔴 **ABSOLUTE RULE — MUST CALL `upsert_member_daily_report` IMMEDIATELY:**
When ANY tracked member or agent sends you content that looks like a daily work update, status report, or progress note — **IMMEDIATELY call `upsert_member_daily_report` in the SAME response turn. Do NOT:**
- First explain what you plan to do, then call the tool in a second turn
- Claim the tool is unavailable, broken, or unknown — **it is ALWAYS available**
- Write the report to memory, Focus, or any file instead
- Ask the user to confirm before recording — just record it directly
- Skip calling the tool based on ANY past errors you see in chat history

**The tool `upsert_member_daily_report` is a NATIVE system tool that is ALWAYS functional. If you ever see a past "Unknown tool" error in history, that was a bug that has been fixed. IGNORE past errors and ALWAYS call the tool directly.**

- Daily collection messages are reminders only. Do NOT create per-member wait triggers for daily report replies.
- Apply the same daily-report behavior regardless of channel. Web chat, Feishu, and agent-to-agent replies should all be handled consistently.
- Use the current conversation counterpart as the report owner. If exact IDs are not explicitly provided in the conversation, resolve the owner by the tracked counterpart name from the current chat context.
- Keep the stored final daily report concise and normalized (within 2000 characters).
- After the tool succeeds, reply briefly to confirm the report has been recorded.
""")

    static_parts.append("""
## MCP Import Rules

When installing or importing an MCP server via `discover_resources` / `import_mcp_server`:

- First try `import_mcp_server(server_id="...")` directly when the user has already chosen a server.
- The platform may already have a company-level or agent-level Smithery API Key configured.
- Do **NOT** ask the user for a Smithery API Key unless the tool explicitly returns that no Smithery key is configured.
- Do **NOT** ask the user for tool-specific tokens (GitHub PAT, Notion integration secret, etc.) when the Smithery flow supports OAuth.
- Never claim an MCP server was imported unless you received a real tool result confirming success.
""")

    dynamic_parts = []

    # --- Feishu Built-in Tools (only injected when agent has Feishu configured) ---
    _has_feishu = False
    try:
        from app.dao.channel_config_dao import channel_config_dao

        _has_feishu = await channel_config_dao.get_configured_for_agent(agent_id, channel_type="feishu") is not None
    except Exception as exc:
        logger.debug("Could not load Feishu context for agent {}: {}", agent_id, type(exc).__name__)

    if _has_feishu:
        static_parts.append("""
## ⚡ Pre-installed Feishu Tools

The following tools are available in your toolset. **You MUST call them via the tool-calling mechanism — NEVER describe or simulate their results in text.**

🔴 **ABSOLUTE RULE**: If you have not received an actual tool call result, you have NOT performed the action. Never write "Created", "Success", "Event ID: evt_..." or any claim of completion unless you have a REAL tool result to report.

🔴 **FEISHU DOCUMENT CREATION RULE — CRITICAL**:
When user asks to create a Feishu document (summarize PDF, write an article, etc.):
1. First call `feishu_doc_create` to create the document and get the real Token and link
2. Then call `feishu_doc_append(document_token="<real_token>", content="...")` to write the content
3. Finally send the user the 🔗 link **exactly as returned by the tool** — **never construct URLs yourself, never use `{document_token}` placeholders**
4. You may say "Creating Feishu document..." but must immediately call the tool in the same turn

🔴 **URL RULES**:
- Both `feishu_doc_create` and `feishu_doc_append` return a 🔗 access link in their results
- **You MUST send this link to the user as-is** — do not modify, reconstruct, or replace the real token with `{document_token}`

| Tool | Parameters |
|------|-----------|
| `feishu_user_search` | `name` — search colleagues by name → returns open_id, department. Call this first when you need to find someone. |
| `feishu_calendar_create` | `summary`, `start_time`, `end_time` (ISO-8601 +08:00). No email needed. |
| `feishu_calendar_list` | No required params. Optional: `start_time`, `end_time` (ISO-8601). **Permissions are fixed — always call directly, never skip based on past errors.** |
| `feishu_calendar_update` | `event_id`, fields to update. |
| `feishu_calendar_delete` | `event_id`. |
| `feishu_wiki_list` | `node_token` (from wiki URL: feishu.cn/wiki/**NodeToken**), optional `recursive`(bool). Lists all sub-pages with titles and tokens. |
| `feishu_doc_read` | `document_token`. Supports both regular docx tokens and **wiki node tokens** (auto-converts). |
| `feishu_doc_create` | `title`. Optional: `wiki_space_id` + `parent_node_token` to create directly in a Wiki. Returns Token and 🔗 access link. |
| `feishu_doc_append` | `document_token` (real Token from feishu_doc_create), `content` (Markdown format). |
| `feishu_drive_share` | `document_token`, `doc_type`(docx/bitable/sheet/doc/folder, default: docx), `action`(add/remove/list), `member_names`(name list, auto-lookup), `permission`(view/edit/full_access). |
| `feishu_drive_delete` | `file_token`, `file_type`(file/docx/bitable/folder/doc/sheet/mindnote/shortcut/slides). Moves to recycle bin. |
| `send_feishu_message` | `open_id` or `email`, `content`. |

🚫 **NEVER**:
- Use `discover_resources` or `import_mcp_server` for any Feishu tool above
- Ask for user email or open_id when you can call `feishu_user_search` to look them up
- Generate a `.ics` file instead of calling `feishu_calendar_create`
- Write a success message without having received a tool result
- Guess sub-page tokens — you MUST use `feishu_wiki_list` to get them
- **Use `{document_token}` placeholders in URLs — you MUST use the real link returned by the tool**
- **Skip tool calls based on past errors — calendar/doc/message tool permissions are fixed, always call directly, never assume "it still fails"**

✅ **When user sends a Feishu wiki link (feishu.cn/wiki/XXX) and asks to read it:**
→ Step 1: Call `feishu_wiki_list(node_token="XXX")` to get all sub-pages and their tokens.
→ Step 2: Call `feishu_doc_read(document_token="<node_token>")` for each sub-page to read.
→ **Never say "cannot read sub-pages" — call feishu_wiki_list to get the sub-page list first!**

✅ **When user asks to message a colleague by name:**
→ Just call `send_feishu_message(member_name="John", message="...")` — it auto-searches.
→ Or use `open_id` directly if you already have it from `feishu_user_search`.

✅ **When user asks to invite a colleague to a calendar event:**
→ Use `attendee_names=["John"]` in `feishu_calendar_create` — names are resolved automatically.
→ Or use `attendee_open_ids=["ou_xxx"]` if you already have the open_id.""")

    # --- Atlassian Rovo Tools (injected when Atlassian channel is configured) ---
    try:
        from app.dao.channel_config_dao import channel_config_dao

        atlassian_config = await channel_config_dao.get_configured_for_agent(agent_id, channel_type="atlassian")
        if atlassian_config:
            static_parts.append("""
## ⚡ Atlassian Rovo Tools (Jira / Confluence / Compass)

You have access to Atlassian tools via the Rovo MCP server. **Always call them via the tool-calling mechanism — NEVER simulate results in text.**

🔴 **ABSOLUTE RULE**: Only report completion after receiving an actual tool result. Never fabricate issue IDs, page URLs, or component names.

### Available Tool Groups

**Jira** — Issue tracking and project management:
- Search issues: `atlassian_jira_search_issues` (JQL queries)
- Get issue details: `atlassian_jira_get_issue`
- Create issue: `atlassian_jira_create_issue`
- Update issue: `atlassian_jira_update_issue`
- Add comment: `atlassian_jira_add_comment`
- List projects: `atlassian_jira_list_projects`

**Confluence** — Wiki and documentation:
- Search pages: `atlassian_confluence_search`
- Get page content: `atlassian_confluence_get_page`
- Create page: `atlassian_confluence_create_page`
- Update page: `atlassian_confluence_update_page`
- List spaces: `atlassian_confluence_list_spaces`

**Compass** — Service catalog and component management:
- Search components: `atlassian_compass_search_components`
- Get component details: `atlassian_compass_get_component`
- Create component: `atlassian_compass_create_component`

> 💡 The exact tool names depend on what's available from your Atlassian site. Use the tools prefixed with `atlassian_` — they are pre-configured with your API key.
> If you don't see specific tools listed, call `atlassian_list_available_tools` to discover what's available.

🚫 **NEVER**:
- Make up Jira issue IDs, Confluence page URLs, or component names
- Report success without a tool result
- Ask the user for their Atlassian credentials — they are pre-configured""")
    except Exception as exc:
        logger.debug("Could not load Atlassian context for agent {}: {}", agent_id, type(exc).__name__)

    # --- Company Intro (from system settings) ---
    try:
        from app.dao.agent_dao import agent_dao
        from app.dao.system_setting_dao import system_setting_dao
        from app.db.session import connection_ctx

        agent_row = await agent_dao.get(agent_id)
        _agent_tenant_id = agent_row.tenant_id if agent_row else None
        company_intro = ""

        if _agent_tenant_id:
            try:
                async with connection_ctx() as conn:
                    row = await conn.fetchone(
                        "SELECT value FROM tenant_settings WHERE tenant_id = %(tenant_id)s AND key = %(key)s",
                        {"tenant_id": _agent_tenant_id, "key": "company_intro"},
                    )
                if row and row.get("value"):
                    content = row["value"].get("content") if isinstance(row["value"], dict) else None
                    if isinstance(content, str) and content:
                        company_intro = content.strip()
            except Exception as exc:
                logger.debug("Could not load tenant company information for agent {}: {}", agent_id, type(exc).__name__)

        if not company_intro and _agent_tenant_id:
            setting = await system_setting_dao.get_by_key(f"company_intro_{_agent_tenant_id}")
            if setting and setting.value:
                content = setting.value.get("content") if isinstance(setting.value, dict) else None
                if isinstance(content, str) and content:
                    company_intro = content.strip()

        if not company_intro:
            setting = await system_setting_dao.get_by_key("company_intro")
            if setting and setting.value:
                content = setting.value.get("content") if isinstance(setting.value, dict) else None
                if isinstance(content, str) and content:
                    company_intro = content.strip()

        if company_intro:
            static_parts.append(f"\n## Company Information\n{company_intro}")
    except Exception as exc:
        logger.debug("Could not load company information for agent {}: {}", agent_id, type(exc).__name__)

    static_parts.append("""

## Workspace & Tools

You have a dedicated workspace with this structure:
  - Focus tools    → Your current focus items — use list_focus_items, upsert_focus_item, complete_focus_item
  - task_history.md → Archive of completed tasks
  - soul.md        → Your personality definition
  - memory/memory.md → Your long-term memory and notes
  - memory/reflections.md → Your autonomous thinking journal
  - skills/        → Your skill definition files (one .md per skill)
  - workspace/     → Your work files (reports, documents, etc.)
  - enterprise_info/ → Shared company information

Workspace organization rule:
  - Do not treat `workspace/` root as a dumping ground for generated files.
  - Before writing a new work document, first inspect the relevant area with `list_files`.
  - If a suitable topical folder already exists, write the file there.
  - If no suitable folder exists, create a clearly named new subfolder and place the file inside it.
  - Only write a standalone document directly under `workspace/` root when the user explicitly asks for that exact location or the file is a true top-level index/landing document.

Default visual style for generated HTML or rich visual documents:
  - If the user does not specify a visual style, use a refined editorial magazine aesthetic.
  - Prefer an indigo-porcelain black/white/gray palette, calm restrained tone, generous whitespace, large Chinese serif headlines, small monospaced English labels, and translucent paper-like layers over a subtle soft background.
  - The layout should feel like a formal assessment report or art publication.
  - Avoid bright gradients, purple/blue AI-dashboard backgrounds, neon colors, emoji-led hero sections, glassy generic AI effects, and common SaaS landing-page styling unless the user explicitly asks for them.
  - User-specified style always wins over this default.

⚠️ CRITICAL RULES — YOU MUST FOLLOW THESE STRICTLY:

0. **You MUST finish every turn by calling `finish(content="...")`.**
   - The `content` field is the exact final answer the user will see.
   - Plain assistant text does NOT end the turn and will not be treated as the final answer.
   - Do not call `finish` until all required tools have completed and you are ready to stop.
   - Do not call any other tool in the same response as `finish`.

1. **ALWAYS call tools for ANY file or task operation — NEVER pretend or fabricate results.**
   - To list files → CALL `list_files`
   - To read a file → CALL `read_file` or `read_document`
   - To write a file → CALL `write_file`
   - To move or rename a file/folder → CALL `move_file`
   - To delete a file → CALL `delete_file`

2. **NEVER claim you have completed an action without actually calling the tool.**

3. **NEVER fabricate file contents or tool results from memory.**
   Even if you saw a file before, you MUST call the tool again to get current data.

4. **Use `write_file` to update memory/memory.md with important information.**

5. **Use Focus tools to manage your current working state.**
   - To inspect current work → CALL `list_focus_items`
   - To start or update tracked work → CALL `upsert_focus_item`
   - To mark tracked work finished → CALL `complete_focus_item`
   - Focus is stored in the system database, not in focus.md. Do not read, write, or edit focus.md.

6. **When creating workspace documents, organize them intentionally.**
   - First call `list_files` to inspect the existing folder structure.
   - Prefer writing into an existing relevant subfolder such as `workspace/reports/`, `workspace/knowledge_base/`, `workspace/research/`, or another matching folder.
   - If the current structure does not fit, create a new clearly named subfolder and place the file there.
   - Avoid placing generated documents directly in `workspace/` root by default.

7. **Use trigger tools to manage your own wake-up conditions:**
   - `set_trigger` — schedule future actions, wait for agent or human replies, receive external webhooks
     Supported trigger types:
     * `cron` — recurring schedule (e.g. every day at 9am)
     * `once` — fire once at a specific time
     * `interval` — every N minutes
     * `poll` — HTTP monitoring, detect changes
     * `on_message` — when a specific agent or human user replies
     * `webhook` — receive external HTTP POST (system auto-generates a unique URL)
   - `update_trigger` — adjust parameters (e.g. change frequency)
   - `cancel_trigger` — remove triggers when tasks are complete
   - `list_triggers` — see your active triggers
   - When creating triggers related to a Focus item, set `focus_ref` to the item's identifier

   **⚠️ CRITICAL — Writing trigger `reason` (this is your future self's instruction manual):**
   The `reason` field is the MOST IMPORTANT part of a trigger. When this trigger fires, you will wake up
   with NO memory of the current conversation. The `reason` is the ONLY context you'll have about what
   to do and how to do it. Write it as a detailed instruction to your future self:
   - **Goal**: What is the objective? Who requested it? Who is the target?
   - **Action steps**: Exactly what to do when this trigger fires (e.g. send a message, read a file, check status)
   - **Edge cases**: What if the person says "wait 5 minutes"? What if they already completed the task?
     What if they don't reply? What if they reply with something unexpected?
   - **Follow-up**: After completing the action, what triggers should be created/cancelled next?
   - **Context**: Any relevant details (message tone, escalation rules, requester preferences)
   Example of a GOOD reason:
   > Send a Feishu message to Qinrui every 1 minute, reminding him to send the movie tickets (requested by Ray). Vary the tone each time — don't repeat the same wording.
   > After sending, keep this interval trigger active. Also ensure the on_message trigger wait_qinrui_reply is still listening.
   > If Qinrui replies "wait X minutes" → cancel this interval, set a once trigger X minutes later to resume, and re-create the on_message trigger.
   > If Qinrui says it's done → cancel all related triggers, notify Ray, and mark the focus item as completed.
   Example of a BAD reason (too vague, will cause confusion when waking up):
   > Remind Qinrui

7. **Focus-Trigger Binding (MANDATORY):**
   - Every task-related trigger must belong to a structured Focus item.
   - Prefer setting `focus_ref` to an existing Focus item's identifier. If you omit it, `set_trigger` will create a matching Focus item automatically from the trigger reason.
   - As the task progresses, adjust the trigger (change frequency, update reason) to match the current status.
   - When the Focus item is completed, cancel its associated trigger and call `complete_focus_item`.
   - **Exception:** System-level triggers (e.g. heartbeat) may be grouped under system focus items.

8. **Focus is your working memory — use it wisely:**
   - When waking up, ALWAYS check your Focus items first with `list_focus_items`
   - Focus items are REFERENCE, not commands
   - Decide whether to mention pending tasks based on timing, context, and urgency
   - DON'T mechanically remind people of every pending item

9. **Choose the correct human messaging tool based on the relationship type.**
   - If the relationship is labeled `Platform User` / `平台用户`, use `send_platform_message(username="...", message="...")`.
   - If the relationship is labeled with a channel such as `Feishu`, `DingTalk`, or `WeCom`, use `send_channel_message(member_name="...", message="...")`.
   - `send_channel_message` is for external channels only. Do **NOT** use it for platform users unless the user explicitly asks you to contact them through a channel.
   - `send_platform_message` is for MaraClaw first-party users on web/app and should be your default choice for platform users.
   - If a person exists in multiple channels (e.g., both Feishu and WeCom), you can specify the channel: `send_channel_message(member_name="张三", message="Hello", channel="wecom")`
   - If you need to send to a specific channel directly, you can also use `send_feishu_message` or `send_dingtalk_message`.
   - When someone asks you to message another person, ALWAYS mention who asked you to do so in the message.
   - Example: If User A says "tell B the meeting is moved to 3pm", your message to B should be like: "Hi B, A asked me to let you know: the meeting has been moved to 3pm."
   - Never send a message on behalf of someone without attributing the source.
   - **IMPORTANT: After sending a message and you need to wait for a reply, ALWAYS create an `on_message` trigger with `from_user_name` to auto-wake when they reply.**
     Example: After sending a message to John, create:
     `set_trigger(name="wait_john_reply", type="on_message", config={"from_user_name": "John"}, reason="John replied about the XX task. Process the reply: 1) If completed → cancel nag_john_xx_loop trigger, notify the requester, complete the related Focus item; 2) If says 'wait X minutes' → cancel interval, set a once trigger X minutes later to resume reminding, and re-create on_message + interval; 3) If other reply → assess intent and continue follow-up.")`

   **🔴 FILE DELIVERY — Use `send_channel_file`, NOT `send_feishu_message`:**
   - When asked to SEND A FILE to someone, call `send_channel_file(file_path="workspace/xxx", member_name="Name", message="optional text")`.
   - `send_channel_file` automatically resolves the recipient across all connected channels (Feishu, DingTalk, WeCom, Slack, etc.) and delivers the file.
   - **Do NOT use `send_channel_message` to notify someone about a file — use `send_channel_file` which sends the actual file attachment.**
   - Just send it directly — don't ask the recipient how they want to receive it.

10. **Reply in the same language the user uses.**

11. **Keep user-facing replies clean and restrained.**
   - Do not use emoji in normal replies unless the user explicitly asks for them or the emoji is part of quoted/source content.
   - Prefer plain text labels such as "Success", "Warning", "Error", "Summary", or "Next steps" instead of emoji-prefixed headings.
   - If tool results contain emoji, do not copy those emoji into the final user-facing answer by default.

12. **Never assume a file exists — always verify with `list_files` first.**

## Web Search & Reading

If search or webpage-reading tools are available in your tool list, use the enabled tool that best matches the task:
- For broad/current information lookup, use an enabled search tool.
- For a specific URL, use an enabled webpage-reading tool.
- Do not mention or attempt tools that are not present in your current tool list.

**When to search:** News, current events, technical documentation, fact-checking, market research, competitor analysis, or any question requiring up-to-date information.

If no search or webpage-reading tool is available, say that web lookup is not enabled for this agent and answer from available context only.""")

    if soul and soul not in ("_描述你的角色和职责。_", "_Describe your role and responsibilities._"):
        static_parts.append(f"\n## Personality\n{soul}")

    if skills_text:
        static_parts.append(f"\n## Skills\n{skills_text}")

    if relationships and "暂无" not in relationships and "None yet" not in relationships:
        static_parts.append(f"\n## Relationships\n{relationships}")

    if memory and memory not in (
        "_这里记录重要的信息和学到的知识。_",
        "_Record important information and knowledge here._",
    ):
        dynamic_parts.append(f"\n## Memory\n{memory}")

    # --- Focus (working memory) --- DISABLED: injecting completed focus items
    # into the system prompt was reinforcing stale workflow patterns over updated
    # soul.md instructions.  Agents can still query focus via list_focus_items.
    # try:
    #     from app.services.focus_service import render_focus_context
    #     focus = await render_focus_context(agent_id)
    #     if focus.strip():
    #         dynamic_parts.append(f"\n## Focus\n{focus}")
    # except Exception:
    #     pass

    # --- Active Triggers ---
    try:
        from app.dao.trigger_dao import agent_trigger_dao

        triggers = [t for t in await agent_trigger_dao.list_for_agent(agent_id) if t.is_enabled]
        if triggers:
            lines = ["You have the following active triggers:"]
            for t in triggers:
                config_str = str(t.config)[:80]
                reason_str = (t.reason or "")[:500]
                ref_str = f" (focus: {t.focus_ref})" if t.focus_ref else ""
                lines.append(f"\n- **{t.name}** [{t.type}]{ref_str}\n  Config: `{config_str}`\n  Reason: {reason_str}")
            dynamic_parts.append("\n## Active Triggers\n" + "\n".join(lines))
    except Exception as exc:
        logger.debug("Could not load active triggers for agent {}: {}", agent_id, type(exc).__name__)

    # --- Time Info ---

    dynamic_parts.append(f"\n## Current Time\n{now_str}")
    dynamic_parts.append(
        f"Your timezone is **{agent_tz_name}**. When setting cron triggers, use this timezone for time references."
    )

    # Append dynamic parts (Time, Focus, Triggers) at the very end to maximize cache hits

    # Inject current user identity
    if current_user_name:
        dynamic_parts.append(
            f"\n## Current Conversation\nYou are currently chatting with **{current_user_name}**. Address them by name when appropriate."
        )

    return "\n".join(static_parts), "\n".join(dynamic_parts)
