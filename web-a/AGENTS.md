# PROJECT KNOWLEDGE BASE

**Package:** `web-a` (MaraClaw admin console)  
**Status:** Bootstrapped SPA shell (2026-08-13)

> Monorepo routing: `../AGENTS.md`.  
> Admin HTTP contracts: `../engine/docs/admin-apis.md`.

## OVERVIEW

Authenticated operator UI for **platform_admin** and **org_admin**. Stack matches monorepo frontend norms (React 19, Vite, Tailwind v4, Radix, Framer Motion, Lucide) plus admin dashboard libs (React Router, TanStack Query/Table, RHF+Zod, Recharts, Sonner).

Auth: JWT in `localStorage` (`maraclaw-admin-token`), session via `AuthProvider`, login at `/login` (`POST /api/auth/login`), bootstrap via `GET /api/auth/me`. Non-admin roles are rejected client-side after successful credential check.

**Genesis / first-login password change:** engine seeds platform admin from `PLATFORM_ADMIN_EMAIL` / `PLATFORM_ADMIN_PASSWORD` and returns `must_change_password` on login/`UserOut`. web-a must:

- Read `must_change_password` from `TokenResponse` / `user` / `identity` (`src/lib/types/auth.ts`).
- After login (and multi-tenant selection), navigate to `/account` when the flag is true.
- `ProtectedRoute` blocks all other console routes until the flag is cleared (`src/routes/protected.tsx`).
- Account page shows a required-change banner; on success call `refreshUser()` after `PUT /api/auth/me/password`.

Password flows: `/forgot-password` + `/reset-password?token=` (public; engine SMTP + `public_base_url` must point reset emails at this app) and signed-in `/account` → `PUT /api/auth/me/password` (new password must differ from current).

**Companies:** `/companies` lists orgs (platform: all; org admin: own). `/companies/:id` manages claimed email domains (`GET/POST/PATCH/DELETE /api/tenants/{id}/email-domains`). System orgs and the default end-user org (OpenClaw) are badged; the fallback org cannot be disabled.

Does **not** own marketing (`web-l`) or end-user chat (`web-e`). Does **not** implement APIs — clients call `engine`.

## STRUCTURE

```
web-a/
├── Dockerfile           # Node 26 build → nginx-unprivileged :8080
├── docker/nginx.conf    # SPA fallback, asset cache, /healthz, CSP
├── .dockerignore
├── vite.config.ts       # port 5174, /api proxy → engine
├── components.json      # shadcn-style aliases
├── .env.example         # VITE_API_BASE_URL, VITE_AUTH_BYPASS
└── src/
    ├── App.tsx          # QueryClient + AuthProvider + Toaster + router
    ├── routes/          # route table + ProtectedRoute
    ├── pages/           # login + feature placeholders
    ├── components/
    │   ├── layout/admin-shell.tsx
    │   ├── ui/          # primitives
    │   └── brand/
    ├── hooks/
    │   ├── use-theme.tsx   # maraclaw-admin-theme
    │   └── use-auth.tsx    # session
    └── lib/
        ├── api.ts       # getApiBaseUrl / apiUrl
        ├── http.ts      # fetch wrapper + ApiError
        ├── auth-api.ts
        ├── auth-storage.ts
        └── types/auth.ts
```

## WHERE TO LOOK

| Task | Location |
|------|----------|
| Login UI | `src/pages/login.tsx` |
| Forgot / reset password | `src/pages/forgot-password.tsx`, `reset-password.tsx` |
| Change password | `src/pages/account.tsx` |
| Auth session | `src/hooks/use-auth.tsx`, `src/lib/auth-api.ts` |
| Route guards | `src/routes/protected.tsx` |
| Nav / shell | `src/components/layout/admin-shell.tsx` |
| Routes | `src/routes/index.tsx` |
| Design tokens | `src/index.css` |
| API base URL | `src/lib/api.ts`, `.env.example` |
| Backend admin APIs | `../engine/docs/admin-apis.md` |
| UI primitives | `src/components/ui/*` (add shadcn-style as needed) |

## CONVENTIONS

- Path alias `@/*` → `src/*`.
- Public env only: `VITE_*`. Never ship engine secrets (including `PLATFORM_ADMIN_PASSWORD`).
- Prefer TanStack Query for server state; forms via RHF + Zod.
- Tenant scoping: platform admin may pass `tenant_id`; org admin is own-tenant only (enforce in API client + UI).
- Treat 403 `{ must_change_password: true }` from engine as force-password-change, not a generic logout.
- Keep brand assets consistent with `web-l` (`public/maraclaw-mark.svg`, warm OKLCH tokens).
- New screens: add under `pages/`, wire in `routes/`, link in shell nav when ready.

## COMMANDS

```bash
npm install
npm run dev
npm run build
npm run lint

docker build -t maraclaw-web-a .
docker run --rm -p 8080:8080 maraclaw-web-a
```

Production image: multi-stage Node **26.7.0** + `nginxinc/nginx-unprivileged` on **8080**. Pass public API origin only via `--build-arg VITE_API_BASE_URL=...` if not using same-origin `/api` proxy.

## ANTI-PATTERNS

- Do not put marketing pages or end-user chat here.
- Do not invent REST handlers in Vite — extend `engine`.
- Do not hardcode absolute API hosts in components; use `apiUrl()`.
- Do not dump admin UI into `web-l` or `web-e`.
- Do not ignore `must_change_password` after login — gated admin APIs will 403 and the operator will look "stuck".
