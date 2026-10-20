# app

Backend application boundary. Nested `AGENTS.md` files own local contracts.

## Entry Points

- `main.py` - `app.main:app`, middleware, router mounts, `/api/health` (pool ping), `/api/version`.
- `main.py` imports `lifespan()` from `runtime/lifecycle.py`: pool initialization, role-gated bootstrap, then realtime/worker/connector work. Ownership/readiness/shutdown: `runtime/AGENTS.md`.
- `config.py` is the only pydantic-settings surface. New env vars need `.env.example` (includes `PLATFORM_ADMIN_EMAIL` / `PLATFORM_ADMIN_PASSWORD`).
- `db/` is the live data layer. Persistence: `records/` dataclasses + `dao/` SQL. There is no `app/models/` or `app/database.py`.
- `templates/` is a **third** template tree: fallback `HEARTBEAT.md` + `reflections.md` copied by `agent_manager` when a workspace is missing them. Not `agent_template/` (scaffold) and not `agent_templates/` (role catalog).

## Local Rules

- Preserve `PROCESS_ROLE` side-effect gating; every process still mounts HTTP routers. `process_roles.py` strips/lowercases/validates roles in Python and Docker `entrypoint.sh` before its case-sensitive predicate. `Bootstrap` runs schema bootstrap; failure aborts unless `ALLOW_MIGRATION_FAILURE=true` explicitly allows continuation.
- Schema is **not** applied in lifespan. Use `python -m app.scripts.bootstrap_db`.
- Bootstrap role order matters: **`ensure_system_orgs()`** (MaraClaw + OpenClaw) → **`ensure_platform_admin()`** → tools/templates/skills → agent seeders. OpenClaw is the default end-user org. Platform admin seed is not optional on greenfield.
- New companies + genesis org admin: `services/tenant_provisioning.py` (called from `POST /api/tenants/` and `POST /api/admin/companies`). Not self-serve.
- Reset/verify email links: `services/frontend_origin.py` (CORS-allowlisted Origin/Referer, else `PUBLIC_BASE_URL`).
- `runtime/diagnostics.py` `log_bwrap_startup_status()` is warn-only.
- Guest sandbox proxy: `SANDBOX_*_PROXY` only. See `services/sandbox/AGENTS.md`.
- Inbound office parse: `services/document_parser/` (anydoc). Outbound HTML/MD/CSV: `services/document_conversion/`.
- Local departments: `services/local_departments.py` + `api/departments.py`, not `templates/`. Provider organization sync is retired; shared member profiles and relationships remain.
- Background work: `app.core.logging.new_trace_id()` before related logs.
- Multi-write paths: `async with connection_ctx():` so DAOs share one commit. CRUD HTTP routers already bind `bind_crud_connection`.
- Connector managers: lifespan `start_all` after the pool. Do not call `start_all` from routes.

## Avoid

- Do not use `create_all` or Alembic. Edit only `scripts/schema_baseline.sql`; initialization is fresh-only, with no upgrade patches.
- Use `app.core.logging`; `logging.getLogger` is restricted to `core/logging/intercept.py`.
- Do not inherit process `HTTP_PROXY` into sandbox guests.
