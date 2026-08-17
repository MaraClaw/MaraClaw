# agent_templates

This is the per-role template catalog seeded into the database. Do not confuse it with `agent_template/`, the singular runtime workspace template copied into every new agent.

## Folder Contract

Each role folder should contain:

- `meta.yaml` with required `name`, `description`, `icon`, and `category`.
- `soul.md`, required and stored as `AgentTemplate.soul_template`.
- `bootstrap.md`, optional first-chat/founding ritual content.

Current catalog shape: 22 role folders; all currently include `bootstrap.md` even though the seeder treats it as optional.

Optional `meta.yaml` fields include `capability_bullets`, `default_skills`, `default_mcp_servers`, and `default_autonomy_policy`.

Use stable kebab-case folder names and stable `meta.yaml` `name` values. Renaming the display name creates a new builtin row and can retire the old one.

## Seeder Behavior

- Startup calls `seed_agent_templates()` from `app.services.template_seeder`.
- `_TEMPLATE_ROOT` is hardcoded by `app/services/template_seeder.py` to repo-root `agent_templates`; this path does not come from `Settings.AGENT_TEMPLATE_DIR`.
- Every direct child directory is treated as a role-template candidate; keep archives and temporary grouping folders outside this tree.
- The seeder loads only these folders.
- Builtins that are no longer in this tree are retired on startup: `agents.template_id` is cleared, then the catalog row is deleted. Agent workspaces are left in place.

## Editing Rules

- Do not create one `AGENTS.md` per role folder; the structure is repetitive and the seeder ignores those files.
- Do not put runtime workspace defaults here. Files copied into new agents belong under singular `agent_template/`.
- Be careful renaming `meta.yaml` `name`; it can create a new builtin template and retire the old one.
- Keep default skills/MCP server names aligned with seeded tools and skills.
- Role template placeholder style uses single braces such as `{name}`, `{user_name}`, and `{user_turns}`; do not copy singular-template `{{agent_name}}` placeholders here.
