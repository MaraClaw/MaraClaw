# PROJECT KNOWLEDGE BASE

**Generated:** 2026-08-16  
**Commit:** 9b09521  
**Branch:** main

> Monorepo router: `../AGENTS.md`. This file is package implementation truth.

## OVERVIEW

Two surfaces, one SPA: public marketing (`/`) plus member workspace (`/app/*`). React 19 + Vite 8 + Tailwind v4 + RR + TanStack Query (workspace only) + RHF/Zod + shadcn-style UI. JWT **`maraclaw-enduser-token`**. Admin console is `web-a`.

## STRUCTURE

```
web-l/
├── index.html              # SPA shell + FOUC theme (`maraclaw-theme`)
├── vite.config.ts          # @ alias, :5173, proxy /api /ws /p → engine
├── components.json         # shadcn new-york / zinc / CSS vars / rsc:false
├── Dockerfile              # Node 26.7 → nginx-unprivileged :8080
├── docker/nginx.conf       # SPA fallback, /healthz, engine proxy, CSP
├── e2e/landing.spec.ts     # Playwright smoke (preview :4173)
├── public/                 # brand marks + nav-icons/ (workspace rail)
└── src/
    ├── main.tsx            # ThemeProvider → App
    ├── App.tsx             # error boundary + AuthProvider + AppRouter + Toaster
    ├── routes/index.tsx    # all routes (there is no src/routes.tsx)
    ├── routes/protected.tsx
    ├── pages/              # landing + auth; workspace in pages/app/
    ├── hooks/              # use-theme, use-auth (rejects platform_admin)
    ├── lib/                # engine clients — see lib/AGENTS.md
    └── components/         # ui / sections / layout / auth / brand / chat
```

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Route table | `src/routes/index.tsx` | Public auth + nested `/app` |
| Auth / org / force-password gates | `routes/protected.tsx`, `hooks/use-auth.tsx` | Rejects `platform_admin`; no tenant → `/join`; `must_change_password` → `/app/account` |
| Landing composition | `pages/landing.tsx` | Hero→Features→Agents→HowItWorks→Integrations→Enterprise→Faq→Cta |
| Login / register / SSO | `pages/login.tsx`, `register.tsx`, `sso-callback.tsx` | Chrome: `components/auth/auth-shell.tsx` |
| Join / transfer | `pages/join-org.tsx`, `transfer.tsx` | Outside `/app`; require a session |
| Workspace screens | `pages/app/*` | Nested `pages/app/AGENTS.md` |
| Workspace chrome + Query | `layout/app-shell.tsx` | **Only** `QueryClientProvider` (not App/main) |
| Agent tabs + outlet `agent` | `layout/agent-layout.tsx` | Start/stop when `access_level === 'manage'` |
| First-run redirect | `layout/onboarding-gate.tsx` | Skip key `maraclaw-onboarding-skipped` |
| Engine HTTP / WS clients | `src/lib/*` | Nested `lib/AGENTS.md` |
| Marketing copy | `components/sections/*` | Nested `sections/AGENTS.md` |
| UI primitives | `components/ui/*` | Nested `ui/AGENTS.md` |
| Theme tokens | `src/index.css` | OKLCH; `container-page` max 72rem |
| Theme FOUC | `index.html` + `hooks/use-theme.tsx` | Key `maraclaw-theme`; meta `#faf8f5` / `#1c1612` |
| Brand mark | `brand/maraclaw-logo.tsx`, `public/maraclaw-mark.svg` | Monorepo source — do not fork |
| Production serve | `Dockerfile`, `docker/nginx.conf` | :8080, non-root, `/healthz` |

## CODE MAP

LSP/codegraph unavailable here — centrality from import graph.

| Symbol | Type | Location | Refs | Role |
|--------|------|----------|------|------|
| `AppRouter` | fn | `routes/index.tsx` | App | Route table |
| `ProtectedRoute` | fn | `routes/protected.tsx` | router | `/app` auth + org + password |
| `AuthProvider` / `useAuth` | ctx | `hooks/use-auth.tsx` | ~17 | Member session |
| `AppShell` | fn | `layout/app-shell.tsx` | router | Workspace chrome + Query |
| `OnboardingGate` | fn | `layout/onboarding-gate.tsx` | router | First-run |
| `AgentLayout` | fn | `layout/agent-layout.tsx` | router | Per-agent tabs + `agent` context |
| `apiRequest` / `ApiError` | fn/class | `lib/http.ts` | ~24 | Only fetch wrapper |
| `workspace-api` | module | `lib/workspace-api.ts` | ~21 | Agent/workspace HTTP (mega) |
| `auth-api` | module | `lib/auth-api.ts` | auth+settings | Auth/tenant HTTP |
| `connectAgentChat` | fn | `lib/chat/ws-client.ts` | chat page | `WS /ws/chat/{id}` |
| `cn` | fn | `lib/utils.ts` | ~21 | Class merge |
| `ThemeProvider` | ctx | `hooks/use-theme.tsx` | main | Theme root |
| `MaraClawLogo` | fn | `brand/maraclaw-logo.tsx` | 4 chrome | Brand source |

## CONVENTIONS

- **Alias:** `@/*` → `src/*` (Vite + tsconfig). Prefer `@/` imports in app code.
- **Files:** kebab-case (`how-it-works.tsx`); components **named** PascalCase exports. Only `App` is default export.
- **No barrels** - import concrete files (`@/components/sections/hero`).
- **React Router** (`BrowserRouter`, not data router). Public: `/login` `/register` `/forgot-password` `/reset-password` `/verify-email` `/sso/callback` `/join` `/transfer`. Product: `/app/*`. Landing hashes stay on `/`. No catch-all 404.
- Vite proxies `/api`, `/ws` (`ws: true`), `/p` → `VITE_DEV_API_PROXY` or `http://127.0.0.1:8000`. Prod nginx same paths → `maraclaw-engine:8000`. `client_max_body_size` 25m.
- **Query** lives in `AppShell` only (`staleTime: 30s`). Marketing/auth do not use it.
- **Tailwind v4 CSS-first** - no `tailwind.config.*` / PostCSS. Tokens live in `index.css` (`@theme inline`, `:root`, `.dark`).
- **Lint:** oxlint (`.oxlintrc.json`), not ESLint/Prettier. `npm run lint`.
- **TS:** `verbatimModuleSyntax` → `import type`. `erasableSyntaxOnly` → no `enum`. Build is `tsc -b && vite build`.
- **shadcn:** new-york, CSS variables, Lucide; hand-maintained under `ui/`.
- **Copy data:** local `const` arrays inside section files - no CMS/API.
- **Motion:** default path = `Reveal`/`Stagger` + `useReducedMotion`. Hero intro uses framer-motion + reduced-motion.
- **Layout width:** `container-page` utility (max 72rem). Display type: `font-display` (Instrument Sans).
- **Product naming:** **MaraClaw** = product; **OpenClaw** = runtime heritage.

## ANTI-PATTERNS (THIS PROJECT)

- Admin UI or privileged `/api/admin/*` (those stay in `web-a`).
- LLM provider, model names, or model pickers (that is `web-a` `/models` for org admins).
- Inventing IM connectors engine does not support (Google Workspace / Email are not channel types; WhatsApp/Discord/Atlassian are incomplete).
- Growing `workspace-api.ts` for a new domain — add `lib/*-api.ts` (Plaza/OKR/control pattern).
- `fetch` in pages — go through `lib/http.ts`. QueryClient on App/main — it belongs in `AppShell`.
- Adding ESLint/Prettier/`tailwind.config` as if they exist — they don’t.
- Long-caching `index.html`; root container; listen on 80 (use 8080).
- Forking the claw mark — reuse `MaraClawLogo` / `public/maraclaw-mark.svg`.

## UNIQUE STYLES

- Warm OKLCH palette (primary hue ~38–42); tokens include `--surface`, `--hero-glow-*`, `--mock-bg`, `--cta-bg`.
- Custom utilities: `container-page`, `text-gradient`, `glow-orb`, `section-band`, `shadow-card`, `shadow-elevated`.
- FOUC-safe theme: inline `index.html` script mirrors React theme (`maraclaw-theme`).
- Button polish: `rounded-xl`, `active:scale-[0.96]`; Badge has extra `soft` variant.
- Docker: Node **26** multi-stage → `nginxinc/nginx-unprivileged`; health `GET /healthz`.

## COMMANDS

```bash
cd web-l
npm install
npm run dev       # Vite :5173, host true
npm run lint      # oxlint
npm run build     # tsc -b && vite build → dist/
npm run preview
npm run test:e2e  # Playwright landing smoke (needs `npx playwright install chromium`)

docker build -t maraclaw-web-l .
docker run --rm -p 8080:8080 maraclaw-web-l
# http://localhost:8080  ·  /healthz
```

## NOTES

- Verification = `npm run build` (+ optional `lint`). e2e is landing/login smoke only — no `/app` or API coverage. No Vitest/Jest.
- Public `VITE_*` only. Empty `VITE_API_BASE_URL` = same-origin. Never set it to `http://0.0.0.0:8000`.
- JWT is `maraclaw-enduser-token`, never `maraclaw-admin-token`. Logout is local (no logout API).
- Password reset / verify emails use request Origin if it is in engine `CORS_ORIGINS`.
- `src/assets/` empty. `public/icons.svg` unused. `@radix-ui/react-navigation-menu` unused — do not add `navigation-menu.tsx` for it.
- Prod CSP `font-src 'self' data:` — Google Fonts in `index.html` fail in the Docker image. External APIs need `connect-src` edits.
- `joinWithInvite` in `auth-api` is unused. `needs_company_setup` is typed, unused.
- Header hash nav omits `#enterprise` (footer has it). Legal footer links all go to `#contact`.
- No CI / Makefile in this checkout.
