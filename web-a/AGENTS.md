# web-a - Admin Console

**Generated:** 2026-10-20
**Commit:** 3942af0
**Branch:** 2026Oct

Parent: `../AGENTS.md`. Follow the owning child guide below for implementation details.

## OVERVIEW

Operator UI for `platform_admin` and `org_admin`: React 19, Vite 8 `:5174`, RR 7, TanStack Query, RHF+Zod, Tailwind v4, Recharts, Sonner.
Live: auth, force-change, companies + claimed domains, Users + org departments, Models, Tools catalog/MCP/configuration, Linkup keys + search analytics.
Engine owns APIs; `web-e` owns marketing and member auth/chat. Do not put those surfaces here or admin screens in `web-e`.

## WHERE TO LOOK

| Concern | Owner |
|---------|-------|
| Boot / theme | `index.html` prevents theme FOUC; `src/main.tsx`: `StrictMode` > `ThemeProvider` > `App` |
| App composition | `src/App.tsx`: `AuthProvider` wraps `AppRouter` + `Toaster` |
| Session / Query lifetime | `src/hooks/use-auth.tsx`: `AuthProvider` owns the session-generation-keyed `QueryClientProvider`, not `App` |
| Paths / access gates | `src/routes/AGENTS.md`: `AppRouter`, `ProtectedRoute`, `PlatformAdminRoute` |
| Screen composition / queries | `src/pages/AGENTS.md`: auth, overview, companies/domains, users/departments, models, search |
| Shared UI / domain components | `src/components/AGENTS.md`: primitives, forms, shells, navigation, brand, theme toggle |
| HTTP / DTOs / API base | `src/lib/AGENTS.md`: `apiRequest`, `ApiError`, `apiUrl`, domain clients |
| Theme / design tokens | `src/hooks/use-theme.tsx`, `src/index.css` |
| Engine admin contract | `../engine/docs/admin-apis.md` |

## AUTH AND TENANCY

- JWT storage: `maraclaw-admin-token`. `AuthProvider` bootstraps `GET /api/auth/me`; reject non-admin roles even after valid credentials.
- OR `must_change_password` from token / `user` / `identity`. Redirect to `/account`; allow `/account`, `/settings` and their subpaths until cleared. After `PUT /api/auth/me/password`, call `refreshUser()`.
- `403 { must_change_password: true }` means force-change, not logout. Keep the token.
- Companies + Search require `PlatformAdminRoute` and `platformAdminOnly` nav; Users + Models + Tools allow both admin roles. Search analytics stays `/search-engine?tab=analytics`, not a new route.
- Org admins stay own-tenant; platform admins may pass `tenant_id`. Only these admin roles configure company LLM pools; members never write keys.
- Theme storage: `maraclaw-admin-theme`. Never reuse `web-e` keys `maraclaw-theme` or `maraclaw-enduser-token`.

## CONVENTIONS AND LIMITS

- Named PascalCase components in kebab-case files; retain the existing default `App.tsx` entry. Alias `@/*` = `src/*`.
- Tailwind v4 is CSS-first (`src/index.css`), not a Tailwind config file. Lint with Oxlint; no ESLint/Prettier config.
- Page data uses TanStack Query; forms use RHF + Zod. New live screens need page + route + nav; shared component rules live in the components guide.
- HTTP belongs in `src/lib/*-api.ts`, not raw `fetch` in pages/components or REST handlers in Vite.
- Public `VITE_*` only; never ship `PLATFORM_ADMIN_*`. No hardcoded API hosts or `VITE_API_BASE_URL=http://0.0.0.0:8000`.
- Keep Disable/Enable visible when `can_disable` is false (MaraClaw + OpenClaw).
- Users manages org departments and memberships. Provider organization sync is retired; `web-e` Directory retains People and Org Departments, without a Synced view.
- `VITE_AUTH_BYPASS` is not implemented. README structure/status and bypass claims are not authoritative.

## COMMANDS AND CHECKS

Run from `web-a/` (no root npm workspace):

```bash
npm ci
npm run dev
npm run lint
npx tsc -b
npm test
npm run build
docker build -t maraclaw-web-a .
docker run --rm -p 8080:8080 maraclaw-web-a
```

`npm test` = `tsc -p tsconfig.tests.json && node --test tests/*.test.ts`: 11 Node test files, not a browser runner.
No npm e2e script exists; the three `tests/*.browser.mjs` scripts are standalone browser checks, outside `npm test` and CI.
`../.github/workflows/frontend-ci.yml`: lint, `tsc -b`, tests, build, Docker build, then final-image `../deploy/smoke_frontend_image.sh`.

## DEVELOPMENT AND PRODUCTION

- `vite.config.ts`: dev `:5174`; `/api` proxies to `VITE_DEV_API_PROXY` or `http://127.0.0.1:8000`. This is dev-only.
- `Dockerfile` + `docker/nginx.conf`: Node 26.12.1 build, nginx-unprivileged static SPA on `:8080`, CSP and `/healthz`; no production `/api` proxy. Route API traffic at the edge.
- `.env.example`: empty `VITE_API_BASE_URL` = same-origin; image override is `--build-arg VITE_API_BASE_URL=...`.
- Reset emails: engine `public_base_url` must point at this admin app.
