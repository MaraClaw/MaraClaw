# MaraClawOne - monorepo guide for agents

**Generated:** 2026-10-23
**Commit:** e009080
**Branch:** 2026Oct

**Audience:** AI coding agents and humans coordinating changes across packages.  
**Product:** MaraClaw - OpenClaw digital employees for teams and companies.

This file is the **repo-level router**. Package contracts live in nested `AGENTS.md`. Prefer the deepest relevant guide once the package is known.

---

## OVERVIEW

Three independently configured packages: FastAPI `engine/` plus two Vite apps, with no root npm workspace. Each package has its own `start-from-docker.sh` launcher. `.github/workflows/` owns CI. Behavioral truth is `engine/`; frontends are clients.

## Package map

| Directory | Role | Audience | Stack | Status |
|-----------|------|----------|-------|--------|
| **`engine/`** | Platform API, workers, connectors | Server | FastAPI, psycopg3, Redis, Python 3.14.8 | Mature |
| **`web-e/`** | End-user web UI (landing + members) | Anonymous + members | React 19, Vite 8 :5173, RR, Query, RHF+Zod | Landing + auth + `/app` workspace (agents, chat, Plaza, OKR, directory, admissions, notifications, Take Control, OpenClaw update notices) |
| **`web-a/`** | Admin console | platform_admin / org_admin | React 19, Vite 8 :5174, RR, TanStack Query, RHF+Zod | Live: login, force-change, companies + domains, Users (org departments), models, tools, broadcast, notifications, activity, admission approvals, OpenClaw updates, Linkup keys + search analytics |

**Rule:** one concern → one package. No admin screens in `web-e`, no marketing in `web-a`, no HTML marketing in `engine`.

```
./
├── AGENTS.md           # THIS FILE - cross-package routing
├── engine/             # Backend - read engine/AGENTS.md before edits
├── web-e/              # End-user UI (landing + members) - web-e/AGENTS.md
├── web-a/              # Admin - web-a/AGENTS.md
└── .github/workflows/  # Engine and frontend verification gates
```

---

## WHERE TO LOOK

| If the change is… | Put it in… | Not in… |
|-------------------|------------|---------|
| Public marketing, SEO, brand, CTA | **`web-e/`** | `web-a`, `engine` |
| Tenant/platform admin UI | **`web-a/`** | `web-e` |
| Member product (auth, join, workspace chat) | **`web-e/`** | `web-a` |
| HTTP/WS, auth, DB, LLM, tools, sandboxes, IM, schema | **`engine/`** | frontends (except clients) |

Full-stack: engine first (API + schema + tests), then the matching UI. Landing never calls privileged APIs.

| Intent | Primary | Also read |
|--------|---------|-----------|
| REST/WS endpoint | `engine` | `engine/AGENTS.md`, `engine/app/api/` |
| Agent role catalog | `engine` | `engine/agent_templates/` ≠ `engine/agent_template/` |
| Tool / sandbox / LLM / web search | `engine` | `engine/app/services/` nested AGENTS; Linkup proxy is `linkup/`; bundled skills live in the sibling guest image project (not a function-calling tool) |
| Inbound office parse (PDF/DOCX/XLSX/PPTX) | `engine` | `app/services/document_parser/` (`firecrawl-anydoc`). Not `document_conversion/` |
| Landing copy / channels list | `web-e` | `web-e/AGENTS.md` — landing shows 12 of 22 `agent_templates/` roles |
| Admin screen | `web-a` | `web-a/AGENTS.md`, `engine/docs/admin-apis.md` |
| Linkup keys / search analytics | `web-a` + `engine` | `web-a` `/search-engine`; `engine/docs/web-search-analytics.md` |
| Company LLM providers / keys | `web-a` + `engine` | `web-a` `/models`; `engine` `/api/enterprise/llm-*`. Members never write keys. |
| Chat / member workspace | `web-e` | `web-e/AGENTS.md` — `/app` agents + `WS /ws/chat/{id}`, Plaza, OKR, directory |
| Org department directory | `engine` | `app/api/departments.py`. Org departments remain; provider organization sync is retired. Assign on `web-a` Users. Agent grants on `web-e` permissions (custom + `use` only) |
| CORS / API base URL | engine config + consuming app | `engine/.env.example` |
| CI checks | `.github/workflows` | `engine-ci.yml`, `frontend-ci.yml` |

---

## Code map

| Symbol | Location | Role |
|--------|----------|------|
| `app` | `engine/app/main.py` | FastAPI assembly and router mounts |
| `lifespan` | `engine/app/runtime/lifecycle.py` | Role-aware startup and owned shutdown |
| `BaseDAO` | `engine/app/dao/base.py` | Shared psycopg persistence boundary |
| `check_agent_access` | `engine/app/core/permissions.py` | Agent authorization |
| `AppRouter` | Each frontend's `src/routes/index.tsx` | Public/protected route trees |

## Package briefs

**`engine/`** - multi-tenant digital-employee platform. Entry: `app/main.py`, `app/config.py`, `app/api/`, `app/services/`, `app/db/` + `dao/` + `records/`, `scripts/schema_baseline.sql`. Role catalog `agent_templates/` ≠ workspace scaffold `agent_template/`. IM: `app/services/channels/` + `docs/channels.md`. Hard rules: psycopg3 only (no SQLAlchemy/Alembic/`app/models/`); `from app.core.logging import logger`; thin routes. Deep map: **`engine/AGENTS.md`**.

```bash
cd engine && ./start-from-docker.sh
cd engine && uv run --extra dev pytest
```

**`web-e/`** - end-user web UI: public landing plus member workspace. JWT `maraclaw-enduser-token`. Live: landing, register / login / verify / reset / SSO, org join/transfer, `/app` agents + live chat + files/tools/channels/schedules + Plaza/OKR/directory + admissions + notifications + OpenClaw update notices + Take Control. Role/channel copy must match engine truth when it claims product facts. Brand source for the monorepo (`MaraClawLogo`, `public/maraclaw-mark.svg`). Guide: **`web-e/AGENTS.md`**. `cd web-e && npm run dev` (:5173, `/api`, `/ws`, and `/p` → engine).

**`web-a/`** - operator console. JWT `maraclaw-admin-token`. Live: login, force-password-change (`must_change_password` → `/account`; `/settings` stays open), companies + claimed email domains, Users (activate members, additional admins, org departments), org-admin LLM pool (`/models`), Tools catalog/MCP/configuration (`/tools`), platform-admin Linkup keys + search analytics (`/search-engine`), broadcast (`/broadcast`), activity log, admission approvals, notifications, OpenClaw updates (`/openclaw-updates`). Guide: **`web-a/AGENTS.md`**. Admin HTTP: `engine/docs/admin-apis.md`. `cd web-a && npm run dev` (:5174, `/api` → engine).

---

## Cross-cutting

**API:** engine routers + tests are behavior truth. Breaking changes: engine tests first, then `web-a` / `web-e` clients.

| Concern | Engine | web-a | web-e |
|---------|--------|-------|-------|
| Session / JWT / SSO | implement | `maraclaw-admin-token` | `maraclaw-enduser-token` |
| Tenant isolation | enforce | select/manage | operate in membership |
| Platform admin | genesis + RBAC | surface | hide (not a member login) |
| First-login password change | `must_change_password` | force `/account`; `/settings` stays open | force `/app/settings`; `/app/account` redirects there |

Genesis PA: `PLATFORM_ADMIN_EMAIL` + `PLATFORM_ADMIN_PASSWORD` at bootstrap (fail-closed on empty DB). Open registration never becomes `platform_admin`. Genesis OA: platform admin via `POST /api/admin/companies` (or `POST /api/tenants/`). JWT may issue before password change; privileged REST/WS/files 403 until cleared. Details: `engine/docs/admin-apis.md`.

**Env:** backend secrets in engine (+ `engine/.env.example`). Frontends: public `VITE_*` only. CORS origins live in **engine**. Fresh installs need `PLATFORM_ADMIN_*`.

**Brand:** **MaraClaw** = product, **OpenClaw** = runtime/guest heritage. Visual source is `web-e` (`MaraClawLogo`, `public/maraclaw-mark.svg`) - do not fork three marks.

### Docs for agents

| Depth | File |
|-------|------|
| Monorepo routing | **`AGENTS.md`** (this file) |
| Backend | `engine/AGENTS.md` + `engine/app/**/AGENTS.md` |
| Admin HTTP + genesis | `engine/docs/admin-apis.md` |
| Admin console | `web-a/AGENTS.md` |
| End-user UI (landing + members) | `web-e/AGENTS.md` |

Keep this file a router. New domains get a nested `AGENTS.md`, not more root prose.

---

## Anti-patterns

| Don’t | Do instead |
|-------|------------|
| Implement REST handlers inside a Vite app | Add routes/services in `engine`, call from UI |
| Put admin pages under `web-e` “for convenience” | Use `web-a` |
| Put marketing copy inside `web-a` | Use `web-e` |
| Edit only landing role cards when changing agent runtime behavior | Change `engine` templates/services; update `web-e` if marketing should match |
| Grow frozen mega-modules in engine (`agent_tools.py`, etc.) | Follow `engine/AGENTS.md` extension points |
| Copy `.env` secrets into frontend code | Public `VITE_*` only |
| Treat `agent_template/` and `agent_templates/` as the same | Scaffold vs seeded catalog - see engine docs |
| Add SQLAlchemy “just for this model” | psycopg3 + DAO + schema baseline |

---

## NOTES

Classify, read the owning guide, implement engine behavior first, then wire the matching UI. Engine CI runs Ruff lint/format, basedpyright, ty, and `bash scripts/test.sh` (scoped coverage). Frontend CI runs lint, TypeScript, `npm test` (Node runner), build, and Docker build. Run package commands from their package directory; CI workflows are the exact command source. No drive-by reformats.

Who is looking, and does it need a secret or privileged role? Public or member → `web-e`. Operator → `web-a`. Data/policy → `engine`.
