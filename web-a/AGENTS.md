# PROJECT KNOWLEDGE BASE

**Generated:** 2026-08-16  
**Commit:** 9b09521  
**Branch:** main

> Monorepo router: `../AGENTS.md`. This file is package implementation truth.

## OVERVIEW

Operator UI for **platform_admin** and **org_admin**. React 19, Vite 8 `:5174`, Tailwind v4, RR 7, TanStack Query, RHF+Zod, Recharts, Sonner.

JWT in `localStorage` (`maraclaw-admin-token`). `AuthProvider` bootstraps `GET /api/auth/me`. Non-admin roles rejected client-side after a successful credential check.

**Force password change:** OR `must_change_password` from token / `user` / `identity`. `ProtectedRoute` allows only `/account` and `/settings` until cleared. After `PUT /api/auth/me/password`, call `refreshUser()`.

**Role split:** Companies + Search sit behind `PlatformAdminRoute` and `platformAdminOnly` nav. Users and Models are both roles. Org admin is own-tenant only. Only org/platform admins configure the company LLM pool; members never write keys.

Does **not** own marketing or member auth (`web-l`). Does **not** implement APIs — clients call `engine`.

## STRUCTURE

```
web-a/
├── Dockerfile           # Node 26.7.0 → nginx-unprivileged :8080
├── docker/nginx.conf    # SPA fallback, /healthz, CSP; no /api proxy
├── vite.config.ts       # :5174, @ → src, /api → VITE_DEV_API_PROXY || :8000
├── .env.example         # VITE_API_BASE_URL (empty = same-origin)
└── src/
    ├── main.tsx         # ThemeProvider → App
    ├── App.tsx          # QueryClient + AuthProvider + Toaster + router
    ├── routes/          # AppRouter, ProtectedRoute, PlatformAdminRoute
    ├── pages/           # see pages/AGENTS.md
    ├── components/
    │   ├── layout/      # AdminShell, AuthShell, NavIcon
    │   ├── companies/   # create form + status icon
    │   ├── brand/       # match web-l mark
    │   └── ui/          # 8 primitives + password-field
    ├── hooks/           # use-auth; use-theme (maraclaw-admin-theme)
    └── lib/             # see lib/AGENTS.md
```

## WHERE TO LOOK

| Task | Location |
|------|----------|
| Login / multi-tenant pick | `src/pages/login.tsx` |
| Forgot / reset password | `src/pages/forgot-password.tsx`, `reset-password.tsx` |
| Change password | `src/pages/account.tsx` |
| Sign out / theme | `src/pages/settings.tsx` |
| Overview + 7-day search snapshot | `src/pages/overview.tsx` |
| Companies / claimed domains | `src/pages/companies.tsx`, `company-detail.tsx`, `src/lib/companies-api.ts` |
| Create company + genesis OA | `src/components/companies/create-company-form.tsx` |
| Users / activate | `src/pages/users.tsx`, `user-detail.tsx`, `src/lib/users-api.ts` |
| LLM models / providers | `src/pages/llm-models.tsx`, `src/lib/llm-models-api.ts` |
| Linkup keys | `src/pages/search-engine.tsx`, `src/lib/linkup-keys-api.ts` |
| Search analytics | `src/pages/search-engine-analytics.tsx` (`?tab=analytics`) |
| Auth session | `src/hooks/use-auth.tsx`, `src/lib/auth-api.ts` |
| Route / PA guards | `src/routes/protected.tsx`, `platform-admin.tsx` |
| Nav / chrome | `src/components/layout/admin-shell.tsx` |
| HTTP / API base | `src/lib/` |
| Design tokens | `src/index.css` |
| Engine admin HTTP | `../engine/docs/admin-apis.md` |

## CODE MAP

LSP/codegraph not configured. Centrality unmeasured (grep import/call sites only).

| Symbol | Type | Location | Role |
|--------|------|----------|------|
| `App` | default | `src/App.tsx` | QueryClient + AuthProvider + router |
| `AppRouter` | fn | `src/routes/index.tsx` | Path table; analytics is query-string, not a route |
| `ProtectedRoute` | fn | `src/routes/protected.tsx` | Auth + admin + force-pw lock |
| `PlatformAdminRoute` | fn | `src/routes/platform-admin.tsx` | `/companies*`, `/search-engine` |
| `AuthProvider` / `useAuth` | hook | `src/hooks/use-auth.tsx` | Session; rejects non-admin |
| `AdminShell` | fn | `src/components/layout/admin-shell.tsx` | Nav; PA-only items; force-pw dims links |
| `apiRequest` / `ApiError` | fn/class | `src/lib/http.ts` | Fetch; 401 clears token only |
| `getApiBaseUrl` / `apiUrl` | fn | `src/lib/api.ts` | Empty / hostname `0.0.0.0` → same-origin |
| `isAdminUser` / `isPlatformAdminUser` | fn | `src/lib/types/auth.ts` | Client RBAC |
| `userMustChangePassword` | fn | `src/lib/types/auth.ts` | Force-pw from `user` flag |

Boot: `index.html` (theme FOUC) → `main.tsx` → `ThemeProvider` → `App` → `AuthProvider` → `AppRouter`.

## CONVENTIONS

- Path alias `@/*` → `src/*`. Lint is **oxlint** (no eslint/prettier config).
- Public `VITE_*` only. Never ship `PLATFORM_ADMIN_*`.
- TanStack Query in **pages only**. Forms: RHF + Zod. New screens: `pages/` + `routes/` + nav when ready.
- Tenant: platform admin may pass `tenant_id`; org admin is own-tenant (client + UI).
- Treat `403 { must_change_password: true }` as force-change, not logout.
- Brand + chrome match `web-l`: `MaraClawLogo`, `public/maraclaw-mark.svg`, warm OKLCH, `bg-card/70` sidebar, footer email + theme + Sign out.
- Theme key `maraclaw-admin-theme`. Token key `maraclaw-admin-token` (never web-l's `maraclaw-theme` / `maraclaw-enduser-token`).
- Verify with `npm run build` (`tsc -b`). No test runner.

## ANTI-PATTERNS

- Marketing pages or member chat here; admin UI in `web-l`.
- REST handlers in Vite; `fetch` from components (add `src/lib/*-api.ts`).
- Hardcoded API hosts; `VITE_API_BASE_URL=http://0.0.0.0:8000`.
- Ignoring `must_change_password` after login.
- Treating `VITE_AUTH_BYPASS` as implemented (README leftover; not in `.env.example` or code).
- Sharing web-l storage keys.
- Hiding Disable/Enable when `can_disable` is false (MaraClaw + OpenClaw).

## COMMANDS

```bash
npm install
npm run dev          # :5174, /api → engine
npm run build        # tsc -b && vite build
npm run lint         # oxlint
docker build -t maraclaw-web-a .
docker run --rm -p 8080:8080 maraclaw-web-a
```

Prod image: Node **26.7.0** → nginx-unprivileged **8080**. Optional `--build-arg VITE_API_BASE_URL=...`. Container does **not** proxy `/api`.

## NOTES

- README structure/status is stale (still says feature screens are placeholders).
- Unused deps: most `@radix-ui/*` plus `@tanstack/react-table` (no `src` imports).
- `http` 401 clears storage but does **not** reset `AuthContext` — the next `refreshUser` / guard must catch it.
- Reset emails: engine `public_base_url` must point at this app.
