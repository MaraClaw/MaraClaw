# PROJECT KNOWLEDGE BASE

**Generated:** 2026-08-12
**Commit:** fb28139a
**Branch:** main
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
│   ├── database.py              # raise-shim; async_session is gone
│   ├── schemas/                 # schemas.py grab-bag + agent_credential
│   ├── scripts/                 # bootstrap_db + one-off modules
│   └── services/                # mixed flat files + runtime packages
├── scripts/                     # schema_baseline.sql + freeze/lint helpers
├── docker/openclaw/             # OpenClaw guest-image helpers (not the API)
├── Dockerfile.openclaw          # Node 26.5 bookworm guest image (arm64)
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
| Settings | `app/config.py`, `.env.example` | Case-sensitive; sandbox proxy is `SANDBOX_*_PROXY` only |
| Logging | `app/core/logging/` | `from app.core.logging import logger` - not loguru |
| DB access | `app/db/`, `app/dao/`, `app/records/` | `connection_ctx` / DAOs. `app/database.py` raises |
| Schema | `scripts/schema_baseline.sql`, `app/scripts/bootstrap_db.py` | Greenfield source of truth; additive `PATCHES` |
| API | `app/api/` | Most use `API_PREFIX`; several self-prefix |
| Tools exec | `agent_tool_exec/`, `tool_definitions/`, `tool_runtime/` | Do not grow `agent_tools.py` |
| LLM | `app/services/llm/` | `caller.py` orchestrates; `client.py` is glue |
| Storage / sandbox / triggers | `storage_runtime/`, `sandbox/`, `trigger_runtime/` | Facades: `storage.py`, `realtime.py` |
| Connectors | `*_stream.py`, `*_gateway.py`, `wechat_channel.py`, `api/google_chat.py` | Lifespan `start_all` after `init_pool` |
| Channel registry / shared helpers | `app/services/channels/` | Types, config CRUD, inbound pipeline; see `docs/channels.md` |
| Templates | `agent_template/` vs `agent_templates/` | Scaffold vs DB catalog - not interchangeable |
| Tests | `tests/` | Fakes + monkeypatch; no live Postgres in CI |
| OpenClaw image | `Dockerfile.openclaw`, `docker/openclaw/` | Guest Node/gogcli image; see `docker/openclaw/AGENTS.md` |

## CODE MAP

| Symbol | Type | Location | Refs | Role |
|---|---|---|---:|---|
| `app` | FastAPI | `app/main.py:390` | broad | App, middleware, mounts, health/version |
| `lifespan` | function | `app/main.py:181` | startup | Pool → seed → realtime/worker/connector |
| `_role_enabled` | function | `app/main.py:31` | startup | Gates `bootstrap`/`api`/`worker`/`connector` |
| `Settings` / `get_settings` | class/fn | `app/config.py` | 100+ | Env contract |
| `init_pool` / `ping_pool` | function | `app/db/pool.py` | startup/health | Process-global psycopg pool |
| `connection_ctx` | cm | `app/db/session.py:29` | DAOs | Commit on success; join if nested |
| `BaseDAO` | class | `app/dao/base.py` | dao | CRUD + record dataclass defaults |
| `check_agent_access` | function | `app/core/permissions.py:280` | API | `(user, agent_id)` - leftover `db` ignored |
| `LoggingService` | class | `app/core/logging/service.py` | broad | Queued process logger |
| `TOOL_HANDLERS` | registry | `app/services/agent_tool_exec/registry.py` | tools | `@register` dispatch |
| `get_sandbox_backend` | function | `app/services/sandbox/registry.py` | tools | Backend factory |
| `get_org_sync_adapter` | function | `app/services/org_sync/factory.py` | identity | Sync adapters ≠ auth providers |

## CONVENTIONS

- Start via `./start-from-sourcecode.sh` or `./start-from-docker.sh`. Python **≥3.14.5** (lock 3.14.6). Ruff `py314`, line 120, double quotes, LF. `uv run --extra dev …`.
- Env names are case-sensitive. `CORS_ORIGINS` is a JSON list; single-quote it in `.env`.
- New DB work: DAOs + `app.db` only. Freeze: `scripts/check_no_new_sqlalchemy.py` (empty allowlist; `app/db/` forbidden).
- Log with `from app.core.logging import logger`. Freeze: `scripts/check_no_direct_loguru.py` (exception: `skill_creator_files/`).
- Pydantic v2: `model_config`, `Field(default_factory=...)`.
- Routes orchestrate; reusable logic goes in services. `PROCESS_ROLE` gates **side effects only** - every process still mounts all routers.
- Multi-write handlers should wrap `async with connection_ctx():` so DAO calls share one commit.

## ANTI-PATTERNS (THIS PROJECT)

- Do not add SQLAlchemy, `app/models/`, or Alembic. Schema changes go in `schema_baseline.sql` + `PATCHES`.
- Do not call `app.database.async_session` (raises). Do not add `logging.getLogger` outside `app/core/logging/intercept.py`.
- Do not import `loguru` in app modules. Do not grow `agent_tools.py`, `tool_seeder.py`, `llm/client.py`, `feishu_service.py`, `auth_provider.py`, `agent_seeder.py`, `okr_reporting.py`, `agentbay_client.py`.
- Do not start process-wide connector/`start_all`/trigger loops from request handlers (one `start_client` after save is the existing exception). Do not start them before `init_pool`.
- Do not inject sandbox guest proxy from process `HTTP_PROXY`. Do not put authenticated proxy URLs on bwrap `--setenv`.
- Do not expose secrets/hashes/cookies in schemas. Do not let non-admins set `allow_network` / tool proxy fields.
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
uv run --extra dev ty check .
uv run python scripts/check_no_new_sqlalchemy.py
uv run python scripts/check_no_direct_loguru.py
uv run pytest
uv run python -m app.scripts.bootstrap_db
```

## NOTES

- CI (`.github/workflows/ci.yml`): ruff, both freeze scripts, ty, pytest. No Docker/schema job. Local `scripts/lint.sh` does **not** run the freeze scripts. CI `ty check .` (no `--force-exclude`).
- Backend `Dockerfile` `pip install`s from `pyproject.toml` (no `uv.lock`). `start-from-docker.sh` forwards `.env` via `-e KEY`, not `--env-file`.
- `entrypoint.sh` runs bootstrap only for `PROCESS_ROLE` containing `all` or `bootstrap` (bash **case-sensitive**). Python `_role_enabled` lowercases. `PROCESS_ROLE=Bootstrap` seeds but skips Docker DDL. Source start always bootstraps unless `SKIP_MIGRATIONS=1`.
- `ALLOW_MIGRATION_FAILURE` wraps **bootstrap_db**, not Alembic.
- Seed failures in lifespan are warnings. Health is a pool ping (503 if down).
- Image may setuid `bwrap` (`BWRAP_SETUID=1`). Local sandbox uses `--unshare-user-try`.
- `pyproject.toml` still lists `asyncpg`; the live pool is psycopg3. Do not add new asyncpg callers.
- `app/services/agent_runtime/` is leftover `__pycache__` only - not a live package. Same for deleted API/service `.pyc` without matching `.py`.
- OpenClaw classifier tests expect **host** Node `v26.5.0`; CI has no Node pin. `docs/refactoring/psycopg-migration.md` is historical dual-stack, not current policy.
- Depth 5–7 under `clawsec_skill_files/` is AGPL payload. Do not add `AGENTS.md` there.
