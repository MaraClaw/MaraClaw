# app

Backend application boundary. Nested `AGENTS.md` files own local contracts.

## Entry Points

- `main.py` - `app.main:app`, middleware, router mounts, `/api/health` (pool ping), `/api/version`.
- `lifespan()`: `init_pool()` first (roles `all|bootstrap|api|worker|connector`), then seeders (including **fail-closed** platform admin), then realtime/worker/connector tasks.
- `config.py` is the only pydantic-settings surface. New env vars need `.env.example` (includes `PLATFORM_ADMIN_EMAIL` / `PLATFORM_ADMIN_PASSWORD`).
- `db/` is the live data layer. Persistence: `records/` dataclasses + `dao/` SQL. There is no `app/models/` or `app/database.py`.
- `templates/` is a **third** template tree: fallback `HEARTBEAT.md` + `reflections.md` copied by `agent_manager` when a workspace is missing them. Not `agent_template/` (scaffold) and not `agent_templates/` (role catalog).

## Local Rules

- Preserve `PROCESS_ROLE` gating. Roles gate **side effects**; every process still serves HTTP. Python lowercases the role; Docker `entrypoint.sh` does not.
- Schema is **not** applied in lifespan. Use `python -m app.scripts.bootstrap_db`.
- Bootstrap role order matters: **`ensure_system_orgs()`** (MaraClaw + OpenClaw) → **`ensure_platform_admin()`** → tools/templates/skills → agent seeders. OpenClaw is the default end-user org. Platform admin seed is not optional on greenfield.
- New companies + genesis org admin: `services/tenant_provisioning.py` (called from `POST /api/tenants/` and `POST /api/admin/companies`). Not self-serve.
- Reset/verify email links: `services/frontend_origin.py` (CORS-allowlisted Origin/Referer, else `PUBLIC_BASE_URL`).
- `_log_bwrap_startup_status()` is warn-only.
- Guest sandbox proxy: `SANDBOX_*_PROXY` only. See `services/sandbox/AGENTS.md`.
- Background work: `app.core.logging.new_trace_id()` before related logs.
- Multi-write paths: `async with connection_ctx():` so DAOs share one commit. CRUD HTTP routers already bind `bind_crud_connection`.
- Connector managers: lifespan `start_all` after the pool. Do not call `start_all` from routes.

## Avoid

- Do not use `create_all` or Alembic. Edit `scripts/schema_baseline.sql` + `PATCHES`.
- Do not add `logging.getLogger` in app code; use `app.core.logging`.
- Do not inherit process `HTTP_PROXY` into sandbox guests.
