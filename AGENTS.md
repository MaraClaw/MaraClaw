# MaraClawOne — monorepo guide for agents

**Audience:** AI coding agents and humans coordinating changes across packages.  
**Product:** MaraClaw — OpenClaw digital employees for teams and companies (multi-tenant, tools, channels, governance).

This file is the **repo-level map**. Package-level detail lives in nested `AGENTS.md` files (especially under `engine/`). Prefer the deepest relevant guide once you know which package owns the work.

---

## Package map

| Directory | Role | Audience | Stack (today) | Status |
|-----------|------|----------|---------------|--------|
| **`engine/`** | Backend services & platform API | Server, workers, connectors | FastAPI, psycopg3, Redis, Python ≥3.14 | Mature |
| **`web-l/`** | Marketing **landing** site | Anonymous / prospects | React 19, Vite, Tailwind v4, Framer Motion, shadcn/ui | Implemented |
| **`web-a/`** | Web UI for **admins** | Tenant/platform operators | TBD (placeholder README only) | Scaffold |
| **`web-e/`** | Web UI for **end users** | People who chat with / manage their agents | TBD (placeholder README only) | Scaffold |

**Rule of thumb:** one concern → one package. Do not put admin screens in `web-e`, end-user chat in `web-a`, marketing sections in either app UI, or HTML marketing pages in `engine`.

```
One/                          # MaraClawOne monorepo root
├── AGENTS.md                 # THIS FILE — cross-package routing
├── README.md
├── .env.example              # Shared env template (often mirrors engine)
├── engine/                   # Backend (API, workers, connectors, agent runtime)
│   └── AGENTS.md             # Backend deep map — read before engine edits
├── web-l/                    # Landing / marketing
├── web-a/                    # Admin console (to be built)
└── web-e/                    # End-user product UI (to be built)
```

---

## Where should new code go?

### 1. Decide by **user** and **surface**

| If the change is… | Put it in… | Not in… |
|-------------------|------------|---------|
| Public marketing, SEO, pricing, brand, demo CTA | **`web-l/`** | `web-a`, `web-e`, `engine` |
| Tenant admin: users, SSO, orgs, agents fleet, tools, quotas, billing-ish ops | **`web-a/`** | `web-e`, `web-l` |
| End-user product: chat, agent roster for members, onboarding as user, personal settings | **`web-e/`** | `web-a`, `web-l` |
| HTTP/WS API, auth, DB, LLM, tools, sandboxes, IM connectors, seeds, schema | **`engine/`** | frontends (except API clients) |
| Shared env contract / secrets template | Root **`.env.example`** and/or **`engine/.env.example`** | Hardcoded in UI |

### 2. Decide by **layer** (full-stack features)

Most features touch more than one package. Split work deliberately:

```
Feature idea
    │
    ├─ Needs new API / data / connector?  → engine first (API + schema + tests)
    │         then wire clients in web-a and/or web-e
    │
    ├─ Admin-only controls?               → web-a (calls engine)
    ├─ Member-facing UX?                  → web-e (calls engine)
    └─ Public story / conversion?         → web-l only (no privileged APIs)
```

| Layer | Package | Examples |
|-------|---------|----------|
| Persistence / schema | `engine` (`scripts/schema_baseline.sql`, DAOs, bootstrap) | New table, column, seed |
| Business logic | `engine` (`app/services/`, `app/api/`) | Autonomy policy, tool exec, onboarding ritual |
| Admin UX | `web-a` | Invite users, manage tenants, configure connectors |
| End-user UX | `web-e` | Chat UI, agent picker, user onboarding |
| Marketing copy / visuals | `web-l` | Hero, agent catalog cards, integrations list |

### 3. Quick intent router

| Intent | Primary package | Also read |
|--------|-----------------|-----------|
| “Add a REST/WS endpoint” | `engine` | `engine/AGENTS.md`, `engine/app/api/` |
| “Change agent role template” | `engine` | `engine/agent_templates/` (catalog) vs `engine/agent_template/` (runtime scaffold) |
| “Fix tool / sandbox / LLM” | `engine` | `engine/app/services/` nested AGENTS |
| “Landing hero / brand / channels list” | `web-l` | `web-l/README.md` |
| “Admin dashboard page” | `web-a` | Create app if still placeholder; do not invent into `web-l` |
| “Chat or user agent workspace UI” | `web-e` | Create app if still placeholder |
| “CORS / auth cookie / API base URL” | `engine` config + the consuming frontend | Root/`.env.example` |

---

## Package briefs

### `engine/` — backend

**Owns:** Multi-tenant digital-employee platform: auth, agents, tools, skills, LLM orchestration, sandboxes, storage, schedules/triggers, IM/identity connectors (Feishu, WeCom, Slack, Discord, Teams-related paths, etc.), admin APIs.

**Key entry points:**

| Concern | Location |
|---------|----------|
| App mount / lifespan | `engine/app/main.py` |
| Settings | `engine/app/config.py`, env examples |
| HTTP routers | `engine/app/api/` |
| Services | `engine/app/services/` |
| DB | `engine/app/db/`, `engine/app/dao/`, `engine/app/records/` |
| Schema | `engine/scripts/schema_baseline.sql`, `engine/app/scripts/bootstrap_db.py` |
| Role catalog (seeded) | `engine/agent_templates/` |
| Runtime workspace scaffold | `engine/agent_template/` |
| Tests | `engine/tests/` |

**Hard constraints (do not fight these):**

- No new SQLAlchemy / Alembic / `app/models/` — pure **psycopg3** + DAOs.
- Log via `from app.core.logging import logger` — not direct loguru in app code.
- Routes orchestrate; logic lives in services.
- Deep detail: **`engine/AGENTS.md`** and nested `AGENTS.md` under `app/`, `services/`, etc.

**Run (from `engine/`):**

```bash
./start-from-sourcecode.sh
# or
./start-from-docker.sh
uv run --extra dev pytest
```

### `web-l/` — landing page

**Owns:** Public marketing site only — brand, positioning, role catalog presentation, integration lists, CTA. **No** authenticated product surfaces.

**Stack:** React 19 + TypeScript + Vite + Tailwind CSS v4 + Framer Motion + shadcn-style UI.

**Layout:**

```
web-l/src/
  components/
    brand/        # MaraClawLogo mark
    layout/       # header, footer
    sections/     # hero, features, agents, …
    ui/           # shadcn-style primitives
  hooks/          # theme, etc.
  lib/
```

**Run:**

```bash
cd web-l && npm install && npm run dev
```

**Coordination notes:**

- Role names/descriptions on the landing page should stay **aligned** with `engine/agent_templates/` when they claim product truth — but landing copy may be shortened for marketing.
- Channel badges (Feishu, WeCom, Slack, Google Chat, Discord, MS Teams, …) should not invent connectors that `engine` never plans to support; when engine adds a channel, update `web-l` only if marketing should show it.
- Do not call privileged admin APIs from `web-l`.

### `web-a/` — admin UI

**Owns (when implemented):** Operator console for organizations/tenants — user/role management, agent fleet admin, tool/skill policy, connector configuration, audit/activity, enterprise settings, platform-admin surfaces.

**Today:** Placeholder (`README.md` only). Scaffolding a new SPA here is correct; **do not** dump admin UI into `web-l` or `web-e`.

**Expected relationship:** Talks only to `engine` HTTP/WS APIs with admin-scoped auth. Prefer shared design tokens/patterns with `web-e` when both exist, but keep packages deployable separately.

### `web-e/` — end-user UI

**Owns (when implemented):** Product experience for members — agent list, chat sessions, onboarding conversations, personal settings, non-admin tool use, notifications as a user.

**Today:** Placeholder (`README.md` only). Scaffold here for member UX; **do not** put chat product UI in `web-l` or admin-only pages in `web-e`.

**Expected relationship:** Talks to `engine` with end-user auth. Admin-only actions belong in `web-a` even if the same engine endpoint exists.

---

## Cross-cutting coordination

### API contract

- **Source of truth for behavior:** `engine` routers + schemas + tests.
- Frontends (`web-a`, `web-e`) are clients. Prefer typed clients generated or hand-written against engine OpenAPI/schemas once apps exist.
- Breaking API changes: update `engine` tests first, then every consuming UI package.

### Auth & tenancy

| Concern | Engine | web-a | web-e | web-l |
|---------|--------|-------|-------|-------|
| Session / JWT / SSO | implement | consume (admin) | consume (user) | none / marketing only |
| Tenant isolation | enforce | select/manage tenant | operate within membership | N/A |
| Platform admin | flags/permissions | surface | hide | N/A |

### Config & env

- Backend secrets and process config: `engine` (+ root `.env.example` when monorepo-wide).
- Frontend public config (API base URL, feature flags for UI): each web package’s own env (`VITE_*` or equivalent) — never ship server secrets to the browser.
- CORS origins for web apps are configured in **engine**, not in the frontends alone.

### Brand & copy

- **Wordmark / mark:** `web-l` brand assets (`MaraClawLogo`, `public/favicon.svg`, `public/maraclaw-mark.svg`) are the current visual source. When `web-a` / `web-e` ship, reuse or extract shared brand assets deliberately — avoid three divergent lobsters/claws.
- Product naming: **MaraClaw** (product), **OpenClaw** (agent runtime heritage). Keep that distinction in UI copy.

### Docs for agents

| Depth | File |
|-------|------|
| Monorepo routing | **`AGENTS.md`** (this file) |
| Backend architecture | `engine/AGENTS.md` |
| Backend subdomains | `engine/app/**/AGENTS.md`, `engine/docs/AGENTS.md`, etc. |
| Landing stack | `web-l/README.md` |

When adding a substantial new package area under `web-a` or `web-e`, add a package-level `AGENTS.md` (or expand README) so this root file can stay a **router**, not a full design dump.

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
| Treat `agent_template/` and `agent_templates/` as the same | Scaffold vs seeded catalog — see engine docs |
| Add SQLAlchemy “just for this model” | psycopg3 + DAO + schema baseline |

---

## Suggested workflow for agents

1. **Classify** the request using the tables above (user × surface × layer).
2. **Open the package `AGENTS.md` / README** for that package before editing.
3. **Implement the source of truth first** (usually `engine` for behavioral features).
4. **Propagate** to the correct frontend(s); skip packages that don’t need the surface.
5. **Verify** in-package (engine: `pytest` / ruff; web-l: `npm run build`; web-a/web-e: once scaffolded).
6. **Keep diffs scoped** — don’t “drive-by” reformat unrelated packages.

---

## Naming cheat-sheet

| Short | Meaning |
|-------|---------|
| `engine` | Backend platform |
| `web-l` | **L**anding |
| `web-a` | **A**dmin UI |
| `web-e` | **E**nd-user UI |
| OpenClaw | Agent runtime lineage / guest image ecosystem |
| MaraClaw | Product / brand for teams & companies |

When unsure where a file belongs, ask: *Who is looking at this UI, and does it require a server secret or privileged role?*  
- Public + no auth → `web-l`  
- Privileged operator → `web-a`  
- Authenticated member product → `web-e`  
- Data, policy, or integration truth → `engine`
