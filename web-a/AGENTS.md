# web-a - Admin Console

**Generated:** 2026-11-05
**Commit:** 0056d92a
**Branch:** 2026November

Parent: `../AGENTS.md`. Follow the owning child guide below for implementation details.

## OVERVIEW

Operator UI for `platform_admin` and `org_admin`: React 19, Vite 8 `:5174`, RR 7, TanStack Query, RHF+Zod, Tailwind v4, Recharts, Sonner.
Live: auth, force-change, companies + claimed domains + deletion, Users (roster export, org departments, agent transfer), Invitations, Models, Tools catalog/MCP/configuration, Tavily keys + search analytics, Broadcast, Notifications, Activity, admission approvals, agent deletion review, transfer payments, Settings (SMTP, transfer policy), OpenClaw updates.
Engine owns APIs; `web-e` owns marketing and member auth/chat. Do not put those surfaces here or admin screens in `web-e`.

## WHERE TO LOOK

| Concern | Owner |
|---------|-------|
| Boot / theme | `index.html` prevents theme FOUC; `src/main.tsx`: `StrictMode` > `ThemeProvider` > `App` |
| App composition | `src/App.tsx`: `AuthProvider` wraps `AppRouter` + `Toaster` |
| Session / Query lifetime | `src/hooks/AGENTS.md`: `AuthProvider` owns the session-generation-keyed `QueryClientProvider`, not `App`; query-option factories and session-fenced mutations |
| Paths / access gates | `src/routes/AGENTS.md`: `AppRouter`, `ProtectedRoute`, role gates, `TransferReviewRoute` |
| Screen composition / queries | `src/pages/AGENTS.md`: auth, companies, users/departments, invitations, review queues, models, search, tools |
| Shared UI / domain components | `src/components/AGENTS.md` (+ `layout/`, `ui/`): primitives, shells, nav eligibility, brand, users widgets |
| HTTP / DTOs / API base | `src/lib/AGENTS.md`: `apiRequest`, session policy, generated contracts, domain clients |
| Theme / design tokens | `src/hooks/use-theme.tsx`, `src/index.css` |
| Tests | `tests/AGENTS.md`: Node contract tests vs standalone browser QA |
| Engine admin contract | `../engine/docs/admin-apis.md` |

## AUTH AND TENANCY

- JWT storage: `maraclaw-admin-token`. `AuthProvider` bootstraps `GET /api/auth/me`; reject non-admin roles even after valid credentials.
- `mustChangePassword` is true when the token response, `user` or `identity` flag says so. `ProtectedRoute` redirects to `/settings`; Settings stays open until cleared and legacy `/account` redirects there. After `PUT /api/auth/me/password`, call `refreshReplacement` with the captured owner and replacement access token.
- Only an authenticated 401 expires the session (see `src/lib/AGENTS.md`). A 403, including `must_change_password`, is a plain `ApiError` and keeps the token.
- Companies + Search + OpenClaw updates require `PlatformAdminRoute` and `platformAdminOnly` nav; Users + Models + Tools allow both admin roles. Search analytics stays `/search-engine?tab=analytics`, not a new route.
- Narrower gates (non-PA org admin for Broadcast and Invitations, genesis for Admissions, active own-org admin for Agent deletions, default-org genesis or PA for Payments) live in `src/lib/types/auth.ts` and `transfer-review-access.ts`; route guard and nav entry must use the same predicate.
- Org admins stay own-tenant; platform admins may pass `tenant_id`. Only these admin roles configure company LLM pools; members never write keys.
- Theme storage: `maraclaw-admin-theme`. Never reuse `web-e` keys `maraclaw-theme` or `maraclaw-enduser-token`.

## CONVENTIONS AND LIMITS

- Named PascalCase components in kebab-case files; retain the existing default `App.tsx` entry. Alias `@/*` = `src/*`.
- Tailwind v4 is CSS-first (`src/index.css`), not a Tailwind config file. Lint with Oxlint; no ESLint/Prettier config.
- Page data uses TanStack Query; forms use RHF + Zod. New live screens need page + route + nav; shared component rules live in the components guide.
- HTTP belongs in `src/lib/*-api.ts`, not raw `fetch` in pages/components or REST handlers in Vite.
- `src/lib/engine-contracts.generated.ts` is generated from `../engine/contracts/api-contract.json`; never hand-edit. Regenerate after engine contract changes.
- Public `VITE_*` only; never ship `PLATFORM_ADMIN_*`. No hardcoded API hosts or `VITE_API_BASE_URL=http://0.0.0.0:8000`.
- Keep Disable/Enable visible when `can_disable` is false (MaraClaw + KABERs).
- Users manages org departments and memberships. Provider organization sync is retired; `web-e` Directory retains People and Org Departments, without a Synced view.
- `VITE_AUTH_BYPASS` is not implemented. README structure/status and bypass claims are not authoritative.

## COMMANDS AND CHECKS

Run from `web-a/` (no root npm workspace):

```bash
npm ci
npm run dev
npm run contracts:generate
npm run contracts:check
npm run lint
npx tsc -b
npm test
npm run build
docker build -t maraclaw-web-a .
docker run --rm --network maraclaw_network -p 8080:8080 maraclaw-web-a
```

`contracts:generate` / `contracts:check` run `../engine/scripts/contract_codegen/generate.mjs` against `../engine/contracts/api-contract.json` into `src/lib/engine-contracts.generated.ts` (`--check` never writes).
`npm test` = `tsc -p tsconfig.tests.json && node --test tests/*.test.ts`: Node runner over `*.test.ts` only. No npm script runs `tests/*.browser.mjs` (standalone browser QA, harnesses, fixtures) and CI does not either; see `tests/AGENTS.md`.
`../.github/workflows/frontend-ci.yml` (web-a + web-e matrix): contract check, lint, `tsc -b`, tests, build, Docker build.

## DEVELOPMENT AND PRODUCTION

- `vite.config.ts`: dev `:5174`; `/api` proxies to `VITE_DEV_API_PROXY` or `http://127.0.0.1:8000`. This is dev-only.
- `Dockerfile` + `docker/nginx.conf`: Node 26.13.0 build, nginx-unprivileged SPA on `:8080`, CSP and `/healthz`; `/api/` proxies to `http://maraclaw-engine:8000` preserving `/api`. Engine must already run with alias `maraclaw-engine` on `maraclaw_network`; run this container with `--network maraclaw_network`.
- `.env.example`: empty `VITE_API_BASE_URL` = same-origin; image override is `--build-arg VITE_API_BASE_URL=...`.
- Reset emails: engine `public_base_url` must point at this admin app.
