# agent_templates

This is the per-role template catalog seeded into the database. Do not confuse it with `agent_template/`, the singular runtime workspace scaffold copied into every new agent.

## Folder Contract

Each role folder contains:

- `meta.yaml` with required `name`, `description`, `icon`, and `category`.
- `soul.md`, required and stored as `AgentTemplate.soul_template`.
- `bootstrap.md`, optional first-chat/founding ritual content; without it `bootstrap_content` is `None` and the shared welcoming prompt applies.

Current catalog: 22 role folders (`tests/test_template_seeder.py` asserts 22 loaded roles); all currently include `bootstrap.md`.

Optional `meta.yaml` fields are `capability_bullets`, `default_skills`, `default_mcp_servers`, and `default_autonomy_policy`; YAML values pass through as written.

Use stable kebab-case folder names and stable `meta.yaml` `name` values. The `name` is the builtin row's identity.

## Seeder Behavior

- Startup runs `seed_agent_templates()` from `app.services.template_seeder` inside the optional-seed block of `app/runtime/bootstrap.py`; a failure logs a warning instead of aborting.
- `_TEMPLATE_ROOT` is hardcoded to repo-root `agent_templates`; it does not come from `Settings.AGENT_TEMPLATE_DIR`.
- Every direct child directory is a candidate; keep archives and temporary grouping folders outside this tree.
- A folder is skipped, with a warning or error log, when `meta.yaml` or `soul.md` is missing, the YAML is malformed or not a mapping, or a required field is absent.
- Retirement compares existing builtins with the names that loaded successfully. A builtin with no loaded template is retired: `agents.template_id` is cleared, then the catalog row is deleted. Agent workspaces are left in place.
- A required-field error in an existing role therefore skips it and retires its previous builtin row on that startup, detaching agents. Fix `meta.yaml` before deploying.
- Loaded roles update their builtin row by `name`; a renamed role creates a new row and retires the old one.

## Editing Rules

- Do not create one `AGENTS.md` per role folder; the structure is repetitive and the seeder ignores those files.
- Do not put runtime workspace defaults here. Files copied into new agents belong under singular `agent_template/`.
- Keep default skills/MCP server names aligned with seeded tools and skills.
- Role template placeholder style uses single braces such as `{name}`, `{user_name}`, and `{user_turns}`; do not copy singular-template `{{agent_name}}` placeholders here.
- Ruff, basedpyright and ty exclude this tree; the loader tests in `tests/test_template_seeder.py` and `tests/test_service_fragment_contracts.py` are the machine check.
