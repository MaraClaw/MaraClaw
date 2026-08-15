# PROJECT KNOWLEDGE BASE

**Generated:** 2026-08-14
**Commit:** 08ac4f7
**Branch:** enhance-web-search
**Mode:** update (init-deep --max-depth=7)

## OVERVIEW

MaraClaw-r2 is a FastAPI backend for an enterprise digital-employee platform: multi-tenant Postgres via **pure psycopg3**, Redis Pub/Sub, agent workspaces, tool execution, LLM orchestration, and optional IM/identity connectors. Backend-only checkout.

## STRUCTURE

```
MaraClaw-r2/
├── app/                         # FastAPI package
│   ├── api/                     # Flat routers mounted from main.py
│   ├── core/                    # auth, permissions, events, logging/
│   ├── db/                      # live psycopg pool/session/errors
│   ├── dao/                     # parameterized SQL repositories
│   ├── records/                 # dataclasses + from_row (not ORM)
│   ├── schemas/                 # schemas.py grab-bag + agent_credential
│   ├── scripts/                 # bootstrap_db + one-off modules
│   └── services/                # mixed flat files + runtime packages
├── scripts/                     # schema_baseline.sql + freeze/lint helpers
├── docker/openclaw/             # OpenClaw guest-image helpers (not the API)
├── Dockerfile.openclaw          # Node 26.7.0 bookworm guest image (arm64)
├── build-openclaw-local-dockerfile.sh / publish-openclaw-local-dockerfile.sh
├── start-from-docker.sh         # builds/runs maraclaw-engine:local
├── agent_template/              # runtime workspace scaffold (copied)
├── agent_templates/             # role catalog seeded into DB
├── docs/                        # operational + refactoring packets
└── tests/                       # root-level pytest; no shared conftest
```

No `alembic/`, no `app/models/`.

## WHERE TO LOOK

| Task | Location | Notes |
|---|---|---|
| Startup / routers | `app/main.py` | `lifespan`, `PROCESS_ROLE`, mounts, `/api/health` |
| Settings | `app/config.py`, `.env.example` | Case-sensitive; sandbox proxy is `SANDBOX_*_PROXY` only; genesis `PLATFORM_ADMIN_*` |
| Genesis platform admin | `app/services/platform_admin_seeder.py` | Env seed at bootstrap; fail-closed if empty DB |
| Tenant + genesis org admin | `app/services/tenant_provisioning.py` | `POST /api/tenants/` and `POST /api/admin/companies` |
| Additional admins | `app/services/admin_provisioning.py` | Genesis PA → more PAs; genesis OA → more OAs; same genesis can activate/deactivate peers |
| Admin action trail | `app/services/admin_audit.py` | `admin_audit_logs`: who / what / when / field changes |
| Admin APIs / RBAC inventory | `docs/admin-apis.md` | Platform vs org admin; genesis + `must_change_password` |
| Auth deps | `app/core/security.py` | JWT, bcrypt, `get_current_user` / force-change gate |
| Logging | `app/core/logging/` | `from app.core.logging import logger` - not loguru |
| DB access | `app/db/`, `app/dao/`, `app/records/` | `connection_ctx` / DAOs |
| Schema | `scripts/schema_baseline.sql`, `app/scripts/bootstrap_db.py` | Greenfield source of truth; additive `PATCHES` |
| API | `app/api/` | Most use `API_PREFIX`; several self-prefix |
| Tools exec | `agent_tool_exec/`, `tool_definitions/`, `tool_runtime/` | Do not grow `agent_tools.py` |
| Page read | `agent_tool_exec/web_read.py` | `read_webpage` only; web lookup/fetch/research/extract are vendored Linkup skills |
| LLM | `app/services/llm/` | `caller.py` orchestrates; `client.py` is glue |
| Storage / sandbox / triggers | `storage_runtime/`, `sandbox/`, `trigger_runtime/` | Facades: `storage.py`, `realtime.py` |
| Connectors | `*_stream.py`, `*_gateway.py`, `wechat_channel.py`, `api/google_chat.py` | Lifespan `start_all` after `init_pool` |
| Channel registry / shared helpers | `app/services/channels/` | Types, config CRUD, inbound pipeline; see `docs/channels.md` |
| Templates | `agent_template/` vs `agent_templates/` | Scaffold vs DB catalog - not interchangeable |
| Tests | `tests/` | Fakes + monkeypatch; no live Postgres in CI |
| OpenClaw image | `Dockerfile.openclaw`, `docker/openclaw/` | Guest Node 26.7 / gogcli 0.36 / OpenClaw 2026.7.1-2; Hub publish is `publish-openclaw-local-dockerfile.sh` |

## CODE MAP

No `codegraph_*` in this harness. LSP `findReferences` + document symbols (2026-08-14).

| Symbol | Type | Location | Refs | Role |
|---|---|---|---:|---|
| `app` | FastAPI | `app/main.py:418` | broad | App, middleware, mounts, health/version |
| `lifespan` | function | `app/main.py:196` | startup | Pool → seed → realtime/worker/connector |
| `_role_enabled` | function | `app/main.py:32` | startup | Gates `bootstrap`/`api`/`worker`/`connector` |
| `Settings` / `get_settings` | class/fn | `app/config.py:81` / `:195` | env | Env contract (`PLATFORM_ADMIN_*`, JWT, …) |
| `ensure_platform_admin` | function | `app/services/platform_admin_seeder.py` | bootstrap | Genesis platform admin from env |
| `create_tenant_with_org_admin` | function | `app/services/tenant_provisioning.py:80` | tenants/admin | Tenant + genesis `org_admin` |
| `load_user_from_access_token` | function | `app/core/security.py:165` | 8+ | JWT → user + identity; force-change gate |
| `init_pool` / `ping_pool` | function | `app/db/pool.py` | startup/health | Process-global psycopg pool |
| `connection_ctx` | cm | `app/db/session.py:29` | 116 | Commit on success; join if nested |
| `BaseDAO` | class | `app/dao/base.py` | dao | CRUD + record dataclass defaults |
| `check_agent_access` | function | `app/core/permissions.py:326` | API | `(user, agent_id)` - leftover `db` ignored |
| `LoggingService` | class | `app/core/logging/service.py` | broad | Queued process logger |
| `TOOL_HANDLERS` | registry | `app/services/agent_tool_exec/registry.py` | tools | `@register` dispatch |
| `get_sandbox_backend` | function | `app/services/sandbox/registry.py` | tools | Backend factory |
| `get_org_sync_adapter` | function | `app/services/org_sync/factory.py` | identity | Sync adapters ≠ auth providers |

## CONVENTIONS

- Start via `./start-from-sourcecode.sh` or `./start-from-docker.sh`. Python **≥3.14.5** (lock 3.14.6). Ruff `py314`, line 120, double quotes, LF. `uv run --extra dev …`.
- Env names are case-sensitive. `CORS_ORIGINS` is a JSON list; single-quote it in `.env`.
- Genesis platform admin: startup loads usable credentials (email + password hash) from the genesis PA in the database. If they are missing, `PLATFORM_ADMIN_EMAIL` + `PLATFORM_ADMIN_PASSWORD` (min 6 chars) seed or repair them. If the env vars are also missing, bootstrap **fails closed**. Open registration never elevates to platform admin.
- New DB work: DAOs + `app.db` only. Freeze: `scripts/check_no_new_sqlalchemy.py` (empty allowlist; `app/db/` forbidden).
- Log with `from app.core.logging import logger`. Freeze: `scripts/check_no_direct_loguru.py` (exception: `skill_creator_files/`).
- Pydantic v2: `model_config`, `Field(default_factory=...)`.
- Routes orchestrate; reusable logic goes in services. `PROCESS_ROLE` gates **side effects only** - every process still mounts all routers.
- Multi-write handlers should wrap `async with connection_ctx():` so DAO calls share one commit.
- Auth: use `get_current_user` for privileged work (enforces `must_change_password`). Use `get_authenticated_user` only for `/auth/me` and password change. Non-Depends paths (WS, file download) must call `load_user_from_access_token`.
- New companies: platform admin only via `POST /api/tenants/` or `POST /api/admin/companies` (`tenant_provisioning`). `POST /api/tenants/self-create` is gone. `allow_self_create_company` does not create tenants.
- Additional platform admins: **genesis** platform admin only (`POST /api/admin/platform-admins`). Additional org admins: **genesis** org admin only (`POST /api/users/org-admins` or `PATCH /api/users/{id}/role`). Genesis is persisted on `users.is_genesis` (not recomputed from `created_at`). Genesis rows cannot be demoted, reassigned, or converted by join.
- Login ignores inactive memberships. Deactivating an org admin does not flip `identity.is_active` (multi-tenant identities). Deactivating a platform admin does.
- Tenant delete tombstones orphaned identities so emails can be reused. `POST /api/tenants/{id}/genesis-org-admin` repairs a tenant that has no genesis OA.

## ANTI-PATTERNS (THIS PROJECT)

- Do not add SQLAlchemy, `app/models/`, or Alembic. Schema changes go in `schema_baseline.sql` + `PATCHES`.
- Do not add `logging.getLogger` outside `app/core/logging/intercept.py`.
- Do not import `loguru` in app modules. Do not grow `agent_tools.py`, `tool_seeder.py`, `llm/client.py`, `feishu_service.py`, `auth_provider.py`, `agent_seeder.py`, `okr_reporting.py`, `agentbay_client.py`.
- Do not start process-wide connector/`start_all`/trigger loops from request handlers (one `start_client` after save is the existing exception). Do not start them before `init_pool`.
- Do not inject sandbox guest proxy from process `HTTP_PROXY`. Do not put authenticated proxy URLs on bwrap `--setenv`.
- Do not expose secrets/hashes/cookies in schemas. Do not let non-admins set `allow_network` / tool proxy fields.
- Do not elevate `platform_admin` via open registration or email-only bootstrap. Do not skip the force-change gate on WS/file-download auth paths.
- Do not treat `skill_creator_files/`, `gogcli_skill_files/`, `clawsec_skill_files/` as normal service code (AGPL-3.0 on ClawSec).
- Do not assume `app/api/whatsapp.py` is mounted.

## UNIQUE STYLES

- Mixed services: flat legacy files + `storage_runtime` / `sandbox` / `trigger_runtime` / `realtime_runtime` / `llm` / `document_conversion`.
- Some routers self-prefix `/api/...` and are included **without** `API_PREFIX` - double-prefix is a real bug class.
- Tests: no `conftest.py`; local fakes; most “API” tests call handlers directly. CI has no Postgres.
- `agent_template/` ≠ `agent_templates/`.

## COMMANDS

```bash
./start-from-sourcecode.sh
SKIP_INSTALL=1 SKIP_MIGRATIONS=1 ./start-from-sourcecode.sh
./start-from-docker.sh

uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uv run --extra dev basedpyright --project pyrightconfig.json app
uv run --extra dev ty check .
uv run python scripts/check_no_new_sqlalchemy.py
uv run python scripts/check_no_direct_loguru.py
uv run pytest                          # enforces 90% on admin/auth/tenant surface
uv run pytest --cov=app --cov-fail-under=0   # full-app report (~39% today)
uv run python -m app.scripts.bootstrap_db
```

## NOTES

- No `.github/workflows` in this checkout. Documented local gates: ruff, basedpyright (`reportAny`), both freeze scripts, ty, pytest. No Docker/schema job. Local `scripts/lint.sh` does **not** run the freeze scripts.
- Backend `Dockerfile` `pip install`s from `pyproject.toml` (no `uv.lock`). `start-from-docker.sh` builds `maraclaw-engine:local`, container `maraclaw-engine`, forwards `.env` via `-e KEY`, not `--env-file`. Setuid bwrap (non-root) needs `SYS_ADMIN` `SETUID` `SETGID` `SYS_CHROOT` `SETPCAP` `NET_ADMIN` `SYS_PTRACE` plus `seccomp=unconfined`. Missing `NET_ADMIN`/`SYS_PTRACE` → `capset failed: Operation not permitted`.
- `entrypoint.sh` runs bootstrap only for `PROCESS_ROLE` containing `all` or `bootstrap` (bash **case-sensitive**). Python `_role_enabled` lowercases. `PROCESS_ROLE=Bootstrap` seeds but skips Docker DDL. Source start always bootstraps unless `SKIP_MIGRATIONS=1`.
- `ALLOW_MIGRATION_FAILURE` wraps **bootstrap_db**, not Alembic.
- Most seed failures in lifespan are warnings. **Exception:** `ensure_platform_admin()` is fail-closed (raises) so greenfield installs cannot serve without a platform admin.
- Platform admin seed runs **before** agent seeders. Genesis platform admin belongs to the **MaraClaw** system org so default agents can seed there. System orgs cannot be disabled.
- Startup also ensures system orgs **MaraClaw** (`maraclaw`) and **OpenClaw** (`openclaw`, default for unmatched end-user registration). It does not rename or reuse a `default` slug. Email domains live in `tenant_email_domains`, not `tenants.sso_domain`. End users may belong to only one tenant; members can transfer with a password confirmation. Domain join/transfer uses a **verified** email only. System and default-end-user orgs cannot be deleted. Join/transfer use `get_current_user` (active + password-change gate).
- Health is a pool ping (503 if down). Image may setuid `bwrap` (`BWRAP_SETUID=1`); local sandbox uses `--unshare-user-try`.
- `pyproject.toml` still lists `asyncpg`; live pool is psycopg3 — no new asyncpg callers. `app/services/agent_runtime/` is gone; do not recreate or add `AGENTS.md` there.
- Three Node pins: guest `26.7.0-bookworm-slim`, sandbox docker `26.5.0-slim`, smoke expects host/guest `v26.7.0`. OpenClaw guest is **linux/arm64 only** (`DOCKERHUB_NAMESPACE=… ./publish-openclaw-local-dockerfile.sh`).
- `docs/refactoring/psycopg-migration.md` is historical dual-stack, not policy. No `AGENTS.md` under `clawsec_skill_files/` skill trees (AGPL payload).
