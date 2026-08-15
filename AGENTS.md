# MaraClawOne - monorepo guide for agents

**Generated:** 2026-08-15  
**Commit:** 04d89c0  
**Branch:** main

**Audience:** AI coding agents and humans coordinating changes across packages.  
**Product:** MaraClaw - OpenClaw digital employees for teams and companies.

This file is the **repo-level router**. Package contracts live in nested `AGENTS.md`. Prefer the deepest relevant guide once the package is known.

---

## OVERVIEW

Loose sibling checkout (no root workspace / turbo / compose / CI). Four packages: FastAPI `engine/` plus three independent Vite apps. Behavioral truth is `engine/`; frontends are clients.

## Package map

| Directory | Role | Audience | Stack | Status |
|-----------|------|----------|-------|--------|
| **`engine/`** | Platform API, workers, connectors | Server | FastAPI, psycopg3, Redis, Python ≥3.14.5 (pin 3.14.7) | Mature |
| **`web-l/`** | Marketing landing | Anonymous | React 19, Vite 8 :5173, Tailwind v4, Framer, shadcn-style | Implemented (no auth/API; Sign in is `#contact`) |
| **`web-a/`** | Admin console | platform_admin / org_admin | React 19, Vite 8 :5174, RR, TanStack Query, RHF+Zod | Live: login, force-change, companies + domains, Linkup keys + search analytics |
| **`web-e/`** | End-user product | Members | React 19, Vite 8 :5175, RR | Live: register / login / join / transfer. Chat not built |

**Rule:** one concern → one package. No admin screens in `web-e`, no member chat in `web-a`, no marketing in either app, no HTML marketing in `engine`.

```
One/
├── AGENTS.md           # THIS FILE - cross-package routing
├── .env.example        # Shared/backend env (mirrors engine)
├── engine/             # Backend - read engine/AGENTS.md before edits
├── web-l/              # Landing - web-l/AGENTS.md
├── web-a/              # Admin - web-a/AGENTS.md
└── web-e/              # Members - web-e/AGENTS.md
```

---

## WHERE TO LOOK

| If the change is… | Put it in… | Not in… |
|-------------------|------------|---------|
| Public marketing, SEO, brand, CTA | **`web-l/`** | `web-a`, `web-e`, `engine` |
| Tenant/platform admin UI | **`web-a/`** | `web-e`, `web-l` |
| Member product (auth, join, future chat) | **`web-e/`** | `web-a`, `web-l` |
| HTTP/WS, auth, DB, LLM, tools, sandboxes, IM, schema | **`engine/`** | frontends (except clients) |
| Shared env / secrets template | Root **`.env.example`** and/or `engine/.env.example` | Hardcoded in UI |

Full-stack: engine first (API + schema + tests), then the matching UI. Landing never calls privileged APIs.

| Intent | Primary | Also read |
|--------|---------|-----------|
| REST/WS endpoint | `engine` | `engine/AGENTS.md`, `engine/app/api/` |
| Agent role catalog | `engine` | `engine/agent_templates/` ≠ `engine/agent_template/` |
| Tool / sandbox / LLM / web search | `engine` | `engine/app/services/` nested AGENTS; Linkup is `linkup/` + `linkup_skill_files/` (not a function-calling tool) |
| Landing copy / channels list | `web-l` | `web-l/AGENTS.md` — landing shows 12 of 22 `agent_templates/` roles |
| Admin screen | `web-a` | `web-a/AGENTS.md`, `engine/docs/admin-apis.md` |
| Linkup keys / search analytics | `web-a` + `engine` | `web-a` `/search-engine`; `engine/docs/web-search-analytics.md` |
| Chat / member workspace | `web-e` | `web-e/AGENTS.md` - chat is not built yet |
| CORS / API base URL | engine config + consuming app | Root `.env.example` |

---

## Package briefs

**`engine/`** - multi-tenant digital-employee platform. Entry: `app/main.py`, `app/config.py`, `app/api/`, `app/services/`, `app/db/` + `dao/` + `records/`, `scripts/schema_baseline.sql`. Role catalog `agent_templates/` ≠ workspace scaffold `agent_template/`. IM: `app/services/channels/` + `docs/channels.md`. Hard rules: psycopg3 only (no SQLAlchemy/Alembic/`app/models/`); `from app.core.logging import logger`; thin routes. Deep map: **`engine/AGENTS.md`**.

```bash
cd engine && ./start-from-sourcecode.sh   # or ./start-from-docker.sh
cd engine && uv run --extra dev pytest
```

**`web-l/`** - public marketing SPA only (hash nav, no API, no `VITE_*`). Role/channel copy must match engine truth when it claims product facts. Brand source for the monorepo (`MaraClawLogo`, `public/maraclaw-mark.svg`). Guide: **`web-l/AGENTS.md`**. `cd web-l && npm run dev` (:5173).

**`web-a/`** - operator console. JWT `maraclaw-admin-token`. Live: login, force-password-change (`must_change_password` → `/account`), companies + claimed email domains, platform-admin Linkup keys + search analytics (`/search-engine`). `/users` and `/tools` still placeholders. Guide: **`web-a/AGENTS.md`**. Admin HTTP: `engine/docs/admin-apis.md`. `cd web-a && npm run dev` (:5174, `/api` → engine).

**`web-e/`** - member product. JWT `maraclaw-enduser-token`. Live: register / login / org join / transfer. Home is a chat placeholder. No force-change UI, no multi-tenant picker. Admin company controls stay in `web-a`. Guide: **`web-e/AGENTS.md`**. `cd web-e && npm run dev` (:5175).

---

## Cross-cutting

**API:** engine routers + tests are behavior truth. Breaking changes: engine tests first, then `web-a` / `web-e` clients.

| Concern | Engine | web-a | web-e | web-l |
|---------|--------|-------|-------|-------|
| Session / JWT / SSO | implement | `maraclaw-admin-token` | `maraclaw-enduser-token` | none |
| Tenant isolation | enforce | select/manage | operate in membership | N/A |
| Platform admin | genesis + RBAC | surface | hide (not a member login) | N/A |
| First-login password change | `must_change_password` | force `/account` | not surfaced yet | N/A |

Genesis PA: `PLATFORM_ADMIN_EMAIL` + `PLATFORM_ADMIN_PASSWORD` at bootstrap (fail-closed on empty DB). Open registration never becomes `platform_admin`. Genesis OA: platform admin via `POST /api/admin/companies` (or `POST /api/tenants/`). JWT may issue before password change; privileged REST/WS/files 403 until cleared. Details: `engine/docs/admin-apis.md`.

**Env:** backend secrets in engine (+ root `.env.example`). Frontends: public `VITE_*` only. CORS origins live in **engine**. Fresh installs need `PLATFORM_ADMIN_*`.

**Brand:** **MaraClaw** = product, **OpenClaw** = runtime/guest heritage. Visual source is `web-l` (`MaraClawLogo`, `public/maraclaw-mark.svg`) - do not fork three marks.

### Docs for agents

| Depth | File |
|-------|------|
| Monorepo routing | **`AGENTS.md`** (this file) |
| Backend | `engine/AGENTS.md` + `engine/app/**/AGENTS.md` |
| Admin HTTP + genesis | `engine/docs/admin-apis.md` |
| Admin console | `web-a/AGENTS.md` |
| Landing | `web-l/AGENTS.md` |
| Member UI | `web-e/AGENTS.md` |

Keep this file a router. New domains get a nested `AGENTS.md`, not more root prose.

---

## Anti-patterns

| Don’t | Do instead |
|-------|------------|
| Implement REST handlers inside a Vite app | Add routes/services in `engine`, call from UI |
| Put admin pages under `web-e` “for convenience” | Use `web-a` |
| Put product chat under `web-l` | Use `web-e` |
| Edit only landing role cards when changing agent runtime behavior | Change `engine` templates/services; update `web-l` if marketing should match |
| Grow frozen mega-modules in engine (`agent_tools.py`, etc.) | Follow `engine/AGENTS.md` extension points |
| Copy `.env` secrets into frontend code | Public `VITE_*` only |
| Treat `agent_template/` and `agent_templates/` as the same | Scaffold vs seeded catalog - see engine docs |
| Add SQLAlchemy “just for this model” | psycopg3 + DAO + schema baseline |

---

## NOTES

`engine` / `web-l` (landing) / `web-a` (admin) / `web-e` (end user). Classify → open that package’s `AGENTS.md` → implement engine first for behavior → wire only the matching UI → verify in-package (`engine`: pytest/ruff; `web-*`: `npm run build`). No drive-by reformats.

Who is looking, and does it need a secret or privileged role? Public → `web-l`. Operator → `web-a`. Member → `web-e`. Data/policy → `engine`.
