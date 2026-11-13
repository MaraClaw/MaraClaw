# agent_template

This is the singular runtime workspace scaffold used to initialize each new agent's storage prefix. It is not the plural `agent_templates/` role catalog.

## Initialization Path

- `Settings.AGENT_TEMPLATE_DIR` defaults to `<repo>/agent_template` from source and `/app/agent_template` in containers.
- `render_agent_files()` in `app/services/agent_workspace.py` reads every file here except `tasks.json`, `todo.json`, `.maraclaw-gateway-key` and anything under `enterprise_info/`, then renders `soul.md` and `state.json`. The loop is file-based, so empty directories need a placeholder such as `.gitkeep`.
- `write_missing_agent_files()` creates each rendered object at `<agent_id>/...` with `create_bytes_if_absent`, under the per-agent `setup_workspace_lock`. Callers: `AgentManager.initialize_agent_files()` (seeders, onboarding) and worker-owned setup in `app/services/agent_setup.py`, which stores the rendered files first and writes them later.
- Initialization is resumable: every missing object is created even when the agent prefix already exists, and an existing prefix never causes an early return. Existing objects are never overwritten, so user edits survive a re-run.
- A template edit therefore reaches an existing agent only for objects it lacks. Pushing changed content to existing workspaces needs a separate backfill.
- `.maraclaw-gateway-key` is never copied from here; the runtime writes it.
- `tasks.json` containing `[]` exists only as the fallback when the template directory itself is missing (with `.gitkeep` under `workspace`, `workspace/knowledge_base`, `memory`, `skills`). A present template never produces `tasks.json`.

## Runtime Files

- `soul.md` is rendered after read. Preserve `{{agent_name}}`, `{{role_description}}`, `{{creator_name}}` and `{{created_at}}`, and keep the `## Personality` and `## Boundaries` headings: creation-time text replaces them, or is appended when absent. `GET /api/agents/default-soul` serves this file unrendered, and a non-blank `AgentCreate.soul` replaces it before rendering, so the placeholders work in creator-edited souls too.
- `state.json` must stay a JSON object. `agent_id` and `name` are overwritten after read; other keys are kept.
- `HEARTBEAT.md` is the periodic awareness contract.
- `memory/` (`MEMORY_INDEX.md`, `curiosity_journal.md`), `workspace/archived/`, `skills/` and `daily_reports/` seed runtime directories and assets. `skills/mcp-installer/SKILL.md` is also read by `skill_seeder`, so edits change the seeded skill.
- When absent here, `memory/memory.md` gets an inline default, and `memory/reflections.md` and `HEARTBEAT.md` come from `app/templates/` (inline fallback if that is missing). A file present here wins.
- Placeholder style here is double-brace runtime substitution, not the single-brace style used in `agent_templates/`.

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
- Do not rely on this template to update existing workspaces; initialization only fills in missing objects.
- Do not put live task state in this directory, and do not add a `tasks.json` or `todo.json`: they are skipped during the copy.
