# MaraClawOne - monorepo guide for agents

**Generated:** 2026-08-15  
**Commit:** 04d89c0  
**Branch:** main

**Audience:** AI coding agents and humans coordinating changes across packages.  
**Product:** MaraClaw - OpenClaw digital employees for teams and companies.

This file is the **repo-level router**. Package contracts live in nested `AGENTS.md`. Prefer the deepest relevant guide once the package is known.

---

## OVERVIEW

Loose sibling checkout (no root workspace / turbo / compose / CI). Three packages: FastAPI `engine/` plus two independent Vite apps. Behavioral truth is `engine/`; frontends are clients.

## Package map

| Directory | Role | Audience | Stack | Status |
|-----------|------|----------|-------|--------|
| **`engine/`** | Platform API, workers, connectors | Server | FastAPI, psycopg3, Redis, Python ≥3.14.5 (pin 3.14.7) | Mature |
| **`web-l/`** | Marketing + member product | Anonymous + members | React 19, Vite 8 :5173, RR, Query, RHF+Zod | Landing + auth + `/app` workspace (agents, chat, Plaza, OKR, directory, Take Control) |
| **`web-a/`** | Admin console | platform_admin / org_admin | React 19, Vite 8 :5174, RR, TanStack Query, RHF+Zod | Live: login, force-change, companies + domains, Linkup keys + search analytics |

**Rule:** one concern → one package. No admin screens in `web-l`, no marketing in `web-a`, no HTML marketing in `engine`.

```
One/
├── AGENTS.md           # THIS FILE - cross-package routing
├── .env.example        # Shared/backend env (mirrors engine)
├── engine/             # Backend - read engine/AGENTS.md before edits
├── web-l/              # Landing + members - web-l/AGENTS.md
└── web-a/              # Admin - web-a/AGENTS.md
```

---

## WHERE TO LOOK

| If the change is… | Put it in… | Not in… |
|-------------------|------------|---------|
| Public marketing, SEO, brand, CTA | **`web-l/`** | `web-a`, `engine` |
| Tenant/platform admin UI | **`web-a/`** | `web-l` |
| Member product (auth, join, future chat) | **`web-l/`** | `web-a` |
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
| Company LLM providers / keys | `web-a` + `engine` | `web-a` `/models`; `engine` `/api/enterprise/llm-*`. Members never write keys. |
| Chat / member workspace | `web-l` | `web-l/AGENTS.md` — `/app` agents + `WS /ws/chat/{id}`, Plaza, OKR, directory |
| CORS / API base URL | engine config + consuming app | Root `.env.example` |

---

## Package briefs

**`engine/`** - multi-tenant digital-employee platform. Entry: `app/main.py`, `app/config.py`, `app/api/`, `app/services/`, `app/db/` + `dao/` + `records/`, `scripts/schema_baseline.sql`. Role catalog `agent_templates/` ≠ workspace scaffold `agent_template/`. IM: `app/services/channels/` + `docs/channels.md`. Hard rules: psycopg3 only (no SQLAlchemy/Alembic/`app/models/`); `from app.core.logging import logger`; thin routes. Deep map: **`engine/AGENTS.md`**.

```bash
cd engine && ./start-from-sourcecode.sh   # or ./start-from-docker.sh
cd engine && uv run --extra dev pytest
```

**`web-l/`** - public marketing plus member workspace. JWT `maraclaw-enduser-token`. Live: landing, register / login / verify / reset / SSO, org join/transfer, `/app` agents + live chat + files/tools/channels + Plaza/OKR/directory + Take Control. Role/channel copy must match engine truth when it claims product facts. Brand source for the monorepo (`MaraClawLogo`, `public/maraclaw-mark.svg`). Guide: **`web-l/AGENTS.md`**. `cd web-l && npm run dev` (:5173, `/api`, `/ws`, and `/p` → engine).

**`web-a/`** - operator console. JWT `maraclaw-admin-token`. Live: login, force-password-change (`must_change_password` → `/account`), companies + claimed email domains, Users (activate members and additional admins), org-admin LLM pool (`/models`), platform-admin Linkup keys + search analytics (`/search-engine`). `/tools` still a placeholder. Guide: **`web-a/AGENTS.md`**. Admin HTTP: `engine/docs/admin-apis.md`. `cd web-a && npm run dev` (:5174, `/api` → engine).

---

## Cross-cutting

**API:** engine routers + tests are behavior truth. Breaking changes: engine tests first, then `web-a` / `web-l` clients.

| Concern | Engine | web-a | web-l |
|---------|--------|-------|-------|
| Session / JWT / SSO | implement | `maraclaw-admin-token` | `maraclaw-enduser-token` |
| Tenant isolation | enforce | select/manage | operate in membership |
| Platform admin | genesis + RBAC | surface | hide (not a member login) |
| First-login password change | `must_change_password` | force `/account` | not surfaced yet |

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
| Landing + member UI | `web-l/AGENTS.md` |

Keep this file a router. New domains get a nested `AGENTS.md`, not more root prose.

---

## Anti-patterns

| Don’t | Do instead |
|-------|------------|
| Implement REST handlers inside a Vite app | Add routes/services in `engine`, call from UI |
| Put admin pages under `web-l` “for convenience” | Use `web-a` |
| Put marketing copy inside `web-a` | Use `web-l` |
| Edit only landing role cards when changing agent runtime behavior | Change `engine` templates/services; update `web-l` if marketing should match |
| Grow frozen mega-modules in engine (`agent_tools.py`, etc.) | Follow `engine/AGENTS.md` extension points |
| Copy `.env` secrets into frontend code | Public `VITE_*` only |
| Treat `agent_template/` and `agent_templates/` as the same | Scaffold vs seeded catalog - see engine docs |
| Add SQLAlchemy “just for this model” | psycopg3 + DAO + schema baseline |

---

## NOTES

`engine` / `web-l` (landing + members) / `web-a` (admin). Classify → open that package’s `AGENTS.md` → implement engine first for behavior → wire only the matching UI → verify in-package (`engine`: pytest/ruff; `web-*`: `npm run build`). No drive-by reformats.

Who is looking, and does it need a secret or privileged role? Public or member → `web-l`. Operator → `web-a`. Data/policy → `engine`.
