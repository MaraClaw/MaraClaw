# web-a/src/routes

Admin path table + RBAC gates. Parent: `../../AGENTS.md`. Screens: `../pages/AGENTS.md`.

## OVERVIEW

`AppRouter` is the path table. Guards wrap; they do not fetch.

| Path | Gate | Screen |
|------|------|--------|
| `/login` `/forgot-password` `/reset-password` | public | auth pages (no shell) |
| `/` | admin | overview |
| `/account` `/settings` | admin; force-pw allowlist | account, settings |
| `/users` `/users/:userId` | admin (PA + OA) | users |
| `/models` | admin (PA + OA) | llm-models |
| `/tools` | admin | `placeholder.tsx` |
| `/companies` `/companies/:companyId` | platform_admin | companies |
| `/search-engine` | platform_admin | search-engine |
| `*` (inside shell) | — | `Navigate` → `/` |

Analytics is `?tab=analytics` on `/search-engine`, not a route.

**Nesting:** public routes bare → `ProtectedRoute` → `AdminShell` → optional `PlatformAdminRoute`.

## WHERE TO LOOK

| File | Role |
|------|------|
| `index.tsx` | `AppRouter`. Public auth vs shell. Analytics is query-string. `/tools` is `PlaceholderPage`. |
| `protected.tsx` | Auth + `isAdmin`. Loading spinner. Unauth / non-admin → `/login`. Force-pw allowlist: `/account` and `/settings` (and `/*` under them) until cleared; else `/account`. |
| `platform-admin.tsx` | `isPlatformAdminUser` else `/`. Wraps `/companies*` and `/search-engine`. |

Nav chrome: `../components/layout/admin-shell.tsx` (`platformAdminOnly`, force-pw dims links). Add the route here first.

## CONVENTIONS

- JWT `maraclaw-admin-token` via `useAuth`. Guards read context, not `localStorage`.
- `403 { must_change_password: true }` is force-change, not logout. Keep the token.
- Non-admin after login → `/login`. Members never see this app (`web-e`).
- New live screen: page + route here + nav. Query-string tabs stay query-string.
- Catch-all is inside the shell, not a second public `*`.

## ANTI-PATTERNS

- `/app` or other member routes (that is `web-e`).
- Implementing APIs or `fetch` in this folder.
- A `/search-engine/analytics` path (keep `?tab=analytics`).
- Metrics or Enterprise-settings routes until engine + page exist. Overview cards are teasers (no `href`).
- Treating `VITE_AUTH_BYPASS` as implemented — it is not.
