# agent_template

This is the singular runtime workspace scaffold copied into each new agent's storage prefix. It is not the plural `agent_templates/` role catalog.

## Copy Path

- `Settings.AGENT_TEMPLATE_DIR` defaults to `<repo>/agent_template` from source and `/app/agent_template` in containers.
- `AgentManager.initialize_agent_files()` uploads every file here into `<agent_id>/...` through the configured storage backend on first agent setup.
- The copy loop is file-based, so empty directories need a placeholder such as `.gitkeep` to survive.
- Existing agent storage is not rewritten when the prefix already exists. Template edits affect newly initialized agents unless a separate backfill is run.
- Files named `tasks.json` or `todo.json`, and paths under `enterprise_info/`, are skipped during the copy.

## Runtime Files

- `soul.md` is customized after copy. Preserve `{{agent_name}}`, `{{role_description}}`, `{{creator_name}}`, and `{{created_at}}`.
- `HEARTBEAT.md` is the periodic awareness contract copied from this directory when present.
- `state.json` is rewritten with the agent id and name after copy; keep it JSON-object shaped.
- `memory/`, `workspace/`, `skills/`, and `daily_reports/` seed runtime directories/assets.
- If `memory/memory.md`, `memory/reflections.md`, or `HEARTBEAT.md` is missing after copy, `AgentManager` creates/fills it from app templates or fallbacks.
- Placeholder style here is double-brace runtime substitution, not the single-brace role-template style used in `agent_templates/`.

## Editing Rules

- Add files here only when every new agent workspace should inherit them.
- Put role/persona catalog content in `agent_templates/<role>/`, not here.
- Keep template files safe for user-visible workspaces. Do not include local secrets, logs, generated caches, or operator-only docs.
- `AGENTS.md` is copied too, so keep this guide safe for agent workspaces as well as contributors.
- Keep bundled skills self-contained under `skills/`; they are workspace runtime assets, not backend services.
- Changes here must stay storage-backend neutral. Avoid local filesystem assumptions.

## Avoid

- Do not add backend-only contributor docs here; use the repo or app-level `AGENTS.md` files.
- Do not add role `meta.yaml`, `bootstrap.md`, or catalog persona files here; those belong in `agent_templates/`.
- Do not rely on this template to update existing workspaces. `initialize_agent_files()` returns early when the agent prefix already exists.
- Do not put live task state in this directory. `tasks.json` and `todo.json` are skipped during the copy.
