# Engine Platform Guide

**Generated:** 2026-10-23
**Commit:** e009080
**Branch:** 2026Oct

Monorepo router: `../AGENTS.md`. This guide owns engine-wide rules; nested guides own local contracts.

FastAPI multi-tenant digital employees: pure psycopg3/Postgres, Redis Pub/Sub, workspaces, tools, LLM orchestration, optional IM/identity connectors. Distribution version comes from `pyproject.toml`; runtime `APP_VERSION` reads `VERSION`. Keep them distinct.

## Navigation And Code Map

| Concern / symbol | Location and contract |
|---|---|
| `app`, `lifespan` | `app/main.py` assembles middleware, flat `app/api/` routers, health/version; imports lifespan from `app/runtime/lifecycle.py`. Read `app/runtime/AGENTS.md`. `/api/health` is a pool ping (503 if down). |
| `Settings`, `get_settings` | `app/config.py`, `.env.example`: case-sensitive settings, JWT/Linkup/genesis env. |
| `init_pool`, `ping_pool`, `connection_ctx`, `BaseDAO` | `app/db/pool.py`, `app/db/session.py`, `app/dao/base.py`: global pool, commit-on-success/nested join, CRUD + `app/records/` dataclass defaults/`from_row`, no ORM. `app/core/events.py` owns Redis events. |
| Auth / authorization | `app/core/security.py`: JWT/bcrypt, `load_user_from_access_token`, `load_user_principal`; `app/core/permissions.py`: `check_agent_access(user, agent_id)`, no DB placeholder. |
| Genesis / company creation | `app/services/platform_admin_seeder.py`: `ensure_platform_admin`; `app/services/tenant_provisioning.py`: `create_tenant_with_org_admin`. |
| Additional admins / audit | `app/services/admin_provisioning.py`, `app/services/admin_audit.py`, `admin_audit_logs`; RBAC inventory: `docs/admin-apis.md`. |
| Org departments | `app/api/departments.py`, `app/services/org_departments.py`, `app/dao/org_department_dao.py`: flat, manually managed tenant directory. Provider organization sync is retired. See `app/api/AGENTS.md`. |
| `logger` | `app/core/logging/`: queued process logger facade. |
| Tools / `TOOL_HANDLERS` | Under `app/services/`: `agent_tool_exec/registry.py` (`@register`), `tool_definitions/`, `agent_tools_definitions/`, `tool_runtime/`; seed vs OpenAI catalogs. `read_webpage`, `search_x` (xAI); Linkup skills handle web search. |
| LLM | `app/services/llm/`: `caller.py` orchestrates, `factory.py` builds registered protocol clients; shared types from `base.py` / `types.py`. |
| Storage / sandbox / workers / channels | Under `app/services/`: mixed flat files + `storage_runtime/`, `sandbox/registry.py` (`get_sandbox_backend`), `trigger_runtime/`, `realtime_runtime/`, `channels/`, `linkup/`, `*_stream.py`, `*_gateway.py`; facades `storage.py`, `realtime.py`. |
| OpenClaw upgrades | `docs/openclaw-updates.md`: global notices (`app/api/openclaw_updates.py`, `openclaw_update_service.py`), gates (`openclaw_update_gate.py`, `openclaw_update_exclusion.py`), worker cutover/rollback (`openclaw_update_executor.py`, `openclaw_update_worker.py`, `openclaw_update_docker.py`, `openclaw_update_snapshot.py`, `openclaw_update_store.py`), `dao/openclaw_update_dao.py`. |
| Documents | `app/services/document_parser/`: inbound local anydoc (`firecrawl-anydoc==0.2.4`), isolated spawn; `app/services/document_conversion/`: outbound conversion. |
| Shared organization members | `org_members` retains identity/contact/profile fields and agent relationship references; provider department hierarchy and people sync are retired. SSO/auth and IM channels remain separate. |
| Templates / scripts | `agent_template/` is copied workspace scaffold; `agent_templates/` is DB-seeded role catalog. `app/scripts/` holds bootstrap/one-offs; `scripts/` holds schema/freeze/lint helpers; `docs/` holds operations/refactoring. |
| Tests | `tests/AGENTS.md`: root-level pytest, no shared `conftest.py`, local fakes/monkeypatch; most API tests call handlers directly, with a separate live-PostgreSQL lifecycle CI job. |

## Engine-Wide Rules

- Python >=3.14.8 (`.python-version`, image `python:3.14.8-slim-trixie`); Ruff `py314`, line 120, double quotes, LF. Pydantic v2: `model_config`, `Field(default_factory=...)`.
- Env names are case-sensitive; `CORS_ORIGINS` is a JSON list, single-quoted in `.env`. New settings belong in `app/config.py` and `.env.example`.
- New DB work uses DAOs + `app.db` only; multi-write handlers use `async with connection_ctx():` for one commit. No SQLAlchemy, `app/models/`, Alembic or `create_all`. `check_no_new_sqlalchemy.py` has an empty allowlist and forbids SQLAlchemy in `app/db/`.
- Schema truth is `scripts/schema_baseline.sql`: fresh-only initialization, idempotent rerun, no upgrade patches. `pyproject.toml` still lists `asyncpg`; add no callers. `docs/refactoring/psycopg-migration.md` is historical dual-stack, not policy.
- Fresh schemas omit `org_departments` and `org_members.department_id`, `department_path`, `synced_at`; shared members, relationships and org departments remain. This source change does not remove existing database objects or data, and has no upgrade patch.
- OpenClaw upgrades: new guest work and guest-visible writes must pass `openclaw_update_gate` / `openclaw_update_exclusion` (typed 423/409); cutover exclusion is the per-agent `setup_workspace_lock` flock, only the `worker` role runs the executor, and ordinary starts honor `agents.openclaw_image_id`. Never call `start_container` for an upgrade without `rehydrate=False`.
- Use `from app.core.logging import logger`; no direct `loguru` in app modules except bundled `skill_creator_files/` (`check_no_direct_loguru.py`). `logging.getLogger` is allowed only in `app/core/logging/intercept.py`.
- Routes orchestrate; reusable logic goes in services. `PROCESS_ROLE` gates side effects only: every process mounts all routers. Self-prefixed `/api/...` routers must be included without `API_PREFIX`.
- Do not grow `agent_tools.py`, `tool_seeder.py`, `feishu_service.py`, `auth_provider.py`, `agent_seeder.py`, `okr_reporting.py`, `agentbay_client.py`. LLM `client.py` is retired: use owning modules. `app/services/agent_runtime/` is gone: do not recreate it or add a guide there.
- No process-wide connector `start_all` / trigger loops from request handlers or before `init_pool`; existing single `start_client` after save is the exception. WhatsApp webhook is mounted; a proactive sender requires registration in `app/services/agent_tool_exec/channel_messaging`.
- Schemas (`app/schemas/schemas.py`, `agent_credential`) must not expose secrets/hashes/cookies. Non-admins cannot set `allow_network` or tool proxy fields.
- Sandbox guests use only `SANDBOX_*_PROXY`, never inherited process `HTTP_PROXY`; never put authenticated proxy URLs on bwrap `--setenv`.
- Privileged work uses `get_current_user` (`must_change_password` gate); `get_authenticated_user` is only for `/auth/me` and password change. Never bypass force-change on WS/download paths.
- Browser downloads use single-use `file_download_tickets`: Bearer `POST /api/agents/{id}/files/download-ticket`; `GET .../files/download` accepts Bearer or `ticket`, never query JWT. Redeem with `load_user_principal(fresh=True)`.
- Chat WebSockets accept only single-use tickets from `POST /api/auth/chat-ticket`; authorize with `load_user_principal(fresh=True)`.
- Genesis PA needs usable DB email + password hash; missing credentials seed/repair from `PLATFORM_ADMIN_EMAIL` + `PLATFORM_ADMIN_PASSWORD` (min 6 chars), otherwise fail closed. No email-only bootstrap or open-registration elevation. Genesis PA belongs to MaraClaw for default-agent seeding.
- Companies + genesis OA: platform admin only via `POST /api/tenants/` or `POST /api/admin/companies` (`tenant_provisioning`). `/api/tenants/self-create` is gone; platform settings expose only invitation/SSO-domain toggles.
- Additional PA: genesis PA only (`POST /api/admin/platform-admins`); additional OA: genesis OA only (`POST /api/users/org-admins`, `PATCH /api/users/{id}/role`). Persist `users.is_genesis`, never infer from `created_at`; no demoting/reassigning/join-converting genesis rows.
- Login ignores inactive memberships. OA deactivation leaves multi-tenant `identity.is_active` unchanged; PA deactivation flips it. End-user deactivation also stops agents/triggers/schedules.
- Tenant deletion tombstones orphan identities for email reuse; `POST /api/tenants/{id}/genesis-org-admin` repairs a missing genesis OA.
- Seed MaraClaw (`maraclaw`) and OpenClaw (`openclaw`, default for unmatched end-user registration); do not rename/reuse `default` slug. System orgs cannot be disabled; system/default-end-user orgs cannot be deleted.
- Email domains live in `tenant_email_domains`, not `tenants.sso_domain`. End users have at most one tenant; transfer needs password confirmation. Domain join/transfer needs verified email and `get_current_user` (active + force-change gate).
- Reset/verify links use `app/services/frontend_origin.py` `resolve_frontend_base_url`: CORS-allowlisted Origin/Referer, else `PUBLIC_BASE_URL`; never an unlisted request host.
- Bundled `skill_creator_files/`, `gogcli_skill_files/`, `clawsec_skill_files/` are not normal service code. ClawSec is AGPL-3.0; retain its existing `AGENTS.md`, never add descendant guides under its skill subtrees.

## Commands (From `engine/`)

```bash
./start-from-docker.sh
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uv run --extra dev basedpyright --project pyrightconfig.json app
uv run --extra dev ty check --force-exclude .
uv run python scripts/check_no_new_sqlalchemy.py
uv run python scripts/check_no_direct_loguru.py
uv run pytest                                  # no implicit coverage
bash scripts/test.sh                          # scoped admin/auth/tenant branch gate: 90%
uv run pytest --cov=app --cov-fail-under=0       # optional full-app report, not the scoped gate
uv run python -m app.scripts.bootstrap_db
```

## Runtime And Images

- `../.github/workflows/engine-ci.yml` runs Ruff, basedpyright, ty and `scripts/test.sh` with explicit scope/terminal report/90% gate; `PYTEST_ADDOPTS` adds XML/HTML. Separate `lifecycle-postgres` uses PostgreSQL 18.6 for setup/retry/authorization, start recovery, deletion/approval cleanup, ownership transfer and guest-config transaction regressions; it does not validate live Docker guests. Local gates include both freeze scripts; `scripts/lint.sh` skips them and uses `ty check --force-exclude`.
- `Dockerfile`: pinned `uv==0.13.6`; `uv export --locked --no-dev --no-emit-project` from `uv.lock`, then `uv pip install --system --require-hashes` (runtime only, no extras). `.dockerignore` recursively excludes secrets/local data/caches but preserves runtime/build inputs including the anydoc fixture.
- `start-from-docker.sh` builds `maraclaw-engine:local`, runs `maraclaw-engine`, forwards `.env` via `-e KEY` (not `--env-file`); mounts host Docker socket + Linux Docker CLI for pywhales/OpenClaw, dual-mounts `DATA_DIR` for host bind paths, creates `DOCKER_NETWORK` (default `maraclaw_network`).
- Bubblewrap 0.12 rejects setuid execution; the image explicitly clears its setuid bit and requires usable host user namespaces for non-root sandboxing. Launcher retains `SYS_ADMIN SETUID SETGID SYS_CHROOT SETPCAP NET_ADMIN SYS_PTRACE` + `seccomp=unconfined`; validate the startup probe and a real backend command, not only file mode. The short probe does not mount `/proc`; a successful probe can still leave the full backend blocked by host permissions. Namespace failure must not trigger host execution.
- `app/process_roles.py` normalizes roles lowercase in Python and `entrypoint.sh` before its case-sensitive predicate: `Bootstrap` DOES run `app.scripts.bootstrap_db`. Only `all`/`bootstrap` run schema bootstrap; failure aborts unless `ALLOW_MIGRATION_FAILURE=true` explicitly permits continuation (not Alembic).
- Runtime bootstrap orders system orgs, fail-closed PA, optional tools/templates/skills, agents; PA precedes agent seeders. `SystemOrgSeedError` and every PA seed failure abort; optional seed blocks warn. Lifecycle/task/readiness ownership: `app/runtime/AGENTS.md`.
- Node 26.12.1 across guest `node:26.12.1-bookworm-slim`, sandbox `node:26.12.1-slim`, frontends `node:26.12.1-trixie-slim`; smoke requires host + guest `v26.12.1`. Guests support linux/amd64 and linux/arm64, gogcli 0.47.1 / OpenClaw 2026.10.7. Photon owns the guest recipe, helpers, build and Hub publish scripts; its local build loads one target and publication builds both under one versioned tag.
