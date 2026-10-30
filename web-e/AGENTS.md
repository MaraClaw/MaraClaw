# web-e - end-user web UI

**Generated:** 2026-10-23
**Commit:** e009080
**Branch:** 2026Oct

Parent: [monorepo router](../AGENTS.md). Prefer the deepest guide below; engine routes/tests remain behavior truth.

## OVERVIEW

Public landing (`/`), member auth/org funnel, and workspace (`/app/*`). React 19 + Vite 8 + React Router + Tailwind v4; TanStack Query is workspace-only, RHF/Zod serve auth forms. Operator UI belongs in `web-a`.

## WHERE TO LOOK

| Task | Source / guide |
|------|----------------|
| SPA assembly | `src/main.tsx`, `src/App.tsx` |
| Routes and gates | `src/routes/index.tsx`, `src/routes/protected.tsx` |
| Landing, auth, join/transfer | [pages/AGENTS.md](./src/pages/AGENTS.md) |
| Member screens and agent tabs | [pages/app/AGENTS.md](./src/pages/app/AGENTS.md) |
| Component families, chrome, brand, primitives | [components/AGENTS.md](./src/components/AGENTS.md), then its layout/sections/ui guides |
| HTTP, sockets, tickets, domain clients | [lib/AGENTS.md](./src/lib/AGENTS.md) |
| Session ownership | `src/lib/auth-session.ts`, `auth-session-controller.ts`; `src/hooks/auth-provider.tsx`, `session-workspace.ts`, `session-query-scope.ts` |
| Theme and shared styles | `index.html`, `src/hooks/theme-provider.tsx`, `src/index.css` |
| Production / verification | `Dockerfile`, `docker/nginx.conf`, `package.json`, `../.github/workflows/frontend-ci.yml` |

## RUNTIME CONTRACTS

- `main.tsx`: ThemeProvider wraps App. `App.tsx`: RootErrorBoundary wraps AuthProvider, AppRouter, and Toaster. Never add QueryClient here or in main; AppShell is the only QueryClientProvider.
- Routing uses BrowserRouter, not a data router. Public auth and session-gated `/join` / `/transfer` stay outside `/app`; landing hashes stay on `/`. No catch-all 404; no `src/routes.tsx`.
- ProtectedRoute sends anonymous users to `/login`, missing-org sessions to `/join` except their `/app/admissions/*` status views, and `must_change_password` to `/app/settings`. `/app/account` redirects there; settings and admissions stay outside OnboardingGate.
- Member auth uses server-enforced member-audience routes and DTOs. `auth-storage.ts` owns localStorage `maraclaw-enduser-token`, never the admin token; logout is local, not an API call. SSO state uses sessionStorage.
- Capture the current session owner for async work; discard stale completions after logout, replacement, or tenant switch. Use session-scoped workspace query/mutation hooks; AppShell creates a per-owner QueryClient and cancels/clears retired-owner work.
- Company join/transfer responses can be pending admission requests: keep the source session and link to `/app/admissions/:id`. Approved requests enter the target only through the existing switch/replacement session flow; KABERs joins/transfers remain immediate.
- Keep the inline FOUC script and ThemeProvider aligned: `maraclaw-theme`, system/light/dark resolution, and theme-color `#faf8f5` / `#1c1612`. ThemeToggle uses the provider, not another theme store.

## CONVENTIONS AND BOUNDARIES

- Prefer `@/` imports (`@/*` maps to `src/*` in Vite and TS). Kebab-case files, named PascalCase component exports; App remains the default-export component. Import concrete files; no new barrels beyond existing ones.
- Tailwind v4 is CSS-first: `src/index.css` owns `@theme inline`, `:root`, `.dark`; no `tailwind.config.*` or PostCSS setup. Shared visual conventions live in the components guide.
- Lint is Oxlint (`.oxlintrc.json`), not ESLint/Prettier. TS uses `verbatimModuleSyntax` (`import type`) and `erasableSyntaxOnly` (no enums).
- Engine I/O goes through `src/lib/http.ts`, never page-level fetch. Add a focused `lib/*-api.ts` for new domains rather than growing `workspace-api.ts`.
- Chat attempts mint fresh HTTP-authenticated single-use tickets; JWTs stay in headers, never socket/file URLs. Files-tab previews and all native downloads mint per action/mount; redemption URLs stay relative/same-origin. Chat attachment previews alone use authenticated metadata/bytes through `http.ts`, with fresh access/version checks on every opening and an owner-scoped bounded memory cache. Detailed lifetimes and stale-preview rules: lib guide.
- No member admin screens or `/api/admin/*`; no LLM provider/model names, pickers, key or subscription controls. Org admins manage these in `web-a` `/models`, including Grok/ChatGPT subscriptions; members inherit the company pool.
- Directory retains People and Org Departments; provider organization sync and the Synced view are retired. Keep org department summaries separate from custom-list agent department grants. Grants are `use`, not `manage`; department creation/membership stays in web-a Users. Member rename/permission rules: workspace and lib guides.
- Landing features 12 of 22 engine role templates. Channel claims must follow engine truth, not badge copy; sections guide records incomplete WhatsApp/Discord/Atlassian support and absent Email/Google Workspace channel types.
- Static assets live in `public/`; no `src/assets/`. `public/icons.svg` is unused; workspace rails use `public/nav-icons/`.

## ENVIRONMENT AND SERVING

- Public `VITE_*` only, never secrets. Empty `VITE_API_BASE_URL` means same-origin; never use `http://0.0.0.0:8000`.
- Vite serves :5173 (`host: true`); `/api`, `/ws` (websocket), `/p` proxy to `VITE_DEV_API_PROXY` or `http://127.0.0.1:8000`. Production proxies these to `maraclaw-engine:8000`, body limit 51m (50 MiB file plus multipart framing).
- Docker builds with Node 26.13.0 and serves via non-root `nginxinc/nginx-unprivileged` on :8080, health `GET /healthz`, SPA fallback. Never long-cache `index.html`, run as root, or switch to port 80.
- CSP `font-src 'self' data:` blocks index.html's Google Fonts in the image; external APIs require `connect-src` edits. Local Office parsing uses only `script-src 'wasm-unsafe-eval'` and `worker-src 'self' blob:`; never add JavaScript `unsafe-eval`, data workers or remote viewer/font hosts. Reset/verify email links prefer engine CORS-allowlisted Origin/Referer, otherwise configured `PUBLIC_BASE_URL`.

## COMMANDS AND CHECKS

Run from `web-e/` (no root npm workspace):
```bash
npm ci
npm run dev
npm run lint
npx tsc -b
npm test           # tsc -p tsconfig.tests.json && node --test tests/*.test.ts
npm run build      # tsc -b && vite build -> dist/
npm run preview
npm run test:e2e    # Playwright; requires Chromium
docker build -t maraclaw-web-e .
docker run --rm -p 8080:8080 maraclaw-web-e
```

`npm test` uses TypeScript checks plus Node's test runner, not Vitest/Jest. Frontend CI runs lint, `tsc -b`, `npm test`, build and Docker build.
Playwright `test:e2e` is separate: `playwright.config.ts` uses `testDir: './e2e'` (currently 32 `*.spec.ts` files, including workspace, auth, chat, schedules and notifications), with a build + preview webServer at `http://127.0.0.1:4173`. `test:e2e` is not run in `frontend-ci.yml`.
