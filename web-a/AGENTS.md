# web-a - Admin Console

**Generated:** 2026-11-16
**Commit:** 86583502
**Branch:** 2026November

Parent: `../AGENTS.md`. Follow the owning child guide below for implementation details.

## OVERVIEW

Operator UI for `platform_admin` and `org_admin`: React 19, Vite 8 `:5174`, RR 7, TanStack Query, RHF+Zod, Tailwind v4, Recharts, Sonner.
Live: auth, force-change, companies + claimed domains + member agent-section visibility + deletion + runtime inventory, Users (roster export, org departments, agent transfer, daily-message display/reset), Invitations, Models, Tools catalog/MCP/configuration, Tavily keys + search analytics, Broadcast, Notifications, Activity, admission approvals, agent deletion review, Clawbook (read-only org posts and comments), Payments (transfers, capacity, organization admissions, payment settings), Settings (SMTP, transfer policy), OpenClaw updates, Upgrade signals (PA resource-pressure summary, episodes, CSV), extra accounts and agents (Users capacity section, `/capacity-orders/:orderId` payment page).
Engine owns APIs; `web-e` owns marketing and member auth/chat. Do not put those surfaces here or admin screens in `web-e`.

## WHERE TO LOOK

| Concern | Owner |
|---------|-------|
| Boot / theme | `index.html` prevents theme FOUC; `src/main.tsx`: `StrictMode` > `ThemeProvider` > `App` |
| App composition | `src/App.tsx`: `TranslationProvider` > `RootErrorBoundary` (reloads on Try again) > `AuthProvider` wraps `AppRouter` + `Toaster` |
| Session / Query lifetime | `src/hooks/AGENTS.md`: `SessionQueryProvider` owns the session-generation-keyed `QueryClientProvider` inside the lazy `SessionShell`, not `AuthProvider` or `App`; query-option factories and session-fenced mutations |
| Paths / access gates | `src/routes/AGENTS.md`: `AppRouter`, `ProtectedRoute`, role gates, `TransferReviewRoute` |
| Screen composition / queries | `src/pages/AGENTS.md`: auth, companies, users/departments, invitations, review queues, models, search, tools |
| Shared UI / domain components | `src/components/AGENTS.md` (+ `layout/`, `ui/`): primitives, shells, nav eligibility, brand, users widgets |
| HTTP / DTOs / API base | `src/lib/AGENTS.md`: `apiRequest`, session policy, generated contracts, domain clients |
| Theme / design tokens | `src/hooks/use-theme.tsx`, `src/index.css` |
| Language / formatting | `src/hooks/use-translation.tsx`: persisted English/Vietnamese; typed messages and translation boundary in `src/lib/AGENTS.md` |
| Tests | `tests/AGENTS.md`: Node contract tests vs standalone browser QA |
| Engine admin contract | `../engine/docs/admin-apis.md` |

## AUTH AND TENANCY

- JWT storage: `maraclaw-admin-token`. `AuthProvider` bootstraps `GET /api/auth/me`; reject non-admin roles even after valid credentials.
- `mustChangePassword` is true when the token response, `user` or `identity` flag says so. `ProtectedRoute` redirects to `/settings`; Settings stays open until cleared and legacy `/account` redirects there. After `PUT /api/auth/me/password`, call `refreshReplacement` with the captured owner and replacement access token.
- Only an authenticated 401 expires the session (see `src/lib/AGENTS.md`). A 403, including `must_change_password`, is a plain `ApiError` and keeps the token.
- Companies (including `/companies/:companyId/runtimes`), Search, OpenClaw updates and Upgrade signals require `PlatformAdminRoute`; their top-level nav entries use `platformAdminOnly`. Users + Models + Tools allow both admin roles. Search analytics stays `/search-engine?tab=analytics`.
- Narrower gates live in `src/lib/types/auth-guards.ts` and `transfer-review-access.ts`: non-PA org admin for Broadcast/Invitations, genesis OA for member Admissions, active own-org admin for Agent deletions and Clawbook, active PA for Payments. Organization admissions and payment settings require an active genesis PA; `/organization-admissions` redirects under its own guard to `/payments?tab=admissions`. Route guards and any matching nav entries must share predicates.
- Capacity route/query gates are documented in `src/routes/AGENTS.md` and `src/hooks/AGENTS.md`. PAs read a company's capacity with `?tenant_id=` and never buy. Payment settings is a separate `/payments?tab=settings` tab; `/settings` retains profile, SMTP, transfer policy and password controls.
- Org admins stay own-tenant; platform admins may pass `tenant_id`. Only these admin roles configure company LLM pools; members never write keys.
- Theme storage: `maraclaw-admin-theme`. Never reuse `web-e` keys `maraclaw-theme` or `maraclaw-enduser-token`.

## CONVENTIONS AND LIMITS

- Named PascalCase components in kebab-case files; retain the existing default `App.tsx` entry. Alias `@/*` = `src/*`.
- Tailwind v4 is CSS-first (`src/index.css`), not a Tailwind config file. Lint with Oxlint; no ESLint/Prettier config.
- Page data uses TanStack Query; forms use RHF + Zod. New routed screens need a page and route; add nav only for a navigation destination. Detail pages such as capacity orders and company runtimes have no separate nav entry. Shared component rules live in the components guide.
- HTTP belongs in `src/lib/*-api.ts`, not raw `fetch` in pages/components or REST handlers in Vite.
- The entry chunk holds the providers, router, session store, `ProtectedRoute` and the route error boundary. Pages, the shell with its session Query scope and motion, the Vietnamese catalog and the auth client (with zod and the generated schemas) load through `retryingLazy`/`retryingImport`. CI caps the entry and its modulepreloads with `bundle-budget.json` (`../.github/scripts/check-bundle-budget.mjs`); `tests/entry-imports.test.ts` fails when zod, react-query, framer-motion, date-fns, tailwind-merge, the auth client or the shell become static imports of `main.tsx`.
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
npm run test:e2e
npm run build
docker build -t maraclaw-web-a .
docker run --rm --network maraclaw_network -p 8080:8080 maraclaw-web-a
```

`contracts:generate` / `contracts:check` run `../engine/scripts/contract_codegen/generate.mjs` against `../engine/contracts/api-contract.json` into `src/lib/engine-contracts.generated.ts` (`--check` never writes).
`npm test` = `tsc -p tsconfig.tests.json && node --test tests/*.test.ts`: Node runner over `*.test.ts` only. `npm run test:e2e` typechecks and runs the selected portable Playwright specs in `e2e/` for company validation/submission, payment alias navigation, admin access and the company runtime workspace measurement status, across phone/desktop and light/dark. It requires an already installed Chromium (optional `CHROMIUM_EXECUTABLE`); it never installs one. No npm script runs `tests/*.browser.mjs` (standalone browser QA, harnesses, fixtures); see `tests/AGENTS.md` and README browser usage.
`../.github/workflows/frontend-ci.yml` (web-a + web-e matrix): contract check, lint, `tsc -b`, tests, build, Docker build.
Its separate `admin-browser` job installs Playwright Chromium/system dependencies and runs the selected admin gate, uploading failure traces/screenshots/HTML reports. The existing four-shard web-e browser job is independent.

## DEVELOPMENT AND PRODUCTION

- `vite.config.ts`: dev `:5174`; `/api` proxies to `VITE_DEV_API_PROXY` or `http://127.0.0.1:8000`. This is dev-only.
- `Dockerfile` + `docker/Caddyfile`: Node 26.13.1 build, Caddy 2.11.9 (alpine) SPA as uid 101 on `:8080`, CSP and `/healthz`; `/api/` proxies to `http://maraclaw-engine:8000` preserving `/api`. The server-wide request body limit is 2MiB with a 10s body idle timeout (`read_body_idle`); only `POST /api/capacity/orders/{uuid}/evidence` (matcher `@evidence`, same `engine_proxy` snippet) gets `request_body max_size 11MiB` and `timeouts { read_timeout 60s }` for the 10 MiB payment evidence upload. Run this container with `--network maraclaw_network`, where the engine has alias `maraclaw-engine`; start order does not matter because Caddy resolves `maraclaw-engine` per connection.
- `.env.example`: empty `VITE_API_BASE_URL` = same-origin; image override is `--build-arg VITE_API_BASE_URL=...`.
- Reset emails: engine `public_base_url` must point at this admin app.
