# pages/ — public + org-funnel

**Generated:** 2026-08-23 · Parent: `web-l/AGENTS.md`

This folder is **public + org-funnel only**. Workspace screens live in `pages/app/` (nested `AGENTS.md`). Do not add `/app` pages here.

## OVERVIEW

Landing + member auth + join/transfer **outside** `/app`. Nine files here; `app/` is a different surface.

## WHERE TO LOOK

| File | Route | Role |
|------|-------|------|
| `landing.tsx` | `/` | Composer only: Hero→Features→Agents→HowItWorks→Integrations→Enterprise→Faq→Cta. Hashes stay on `/`. Copy in `@/components/sections/*`. |
| `login.tsx` | `/login` | RHF+Zod. `useAuth().login`. Multi-tenant picker; unverified → `/verify-email`. Authed → `/join` or `from`. |
| `register.tsx` | `/register` | `fetchRegistrationConfig` (invite gate), `checkDuplicate`, `registerRequest`. Cannot mint `platform_admin`. |
| `sso-callback.tsx` | `/sso/callback` | `oauthCallback` or `bindProvider` (`SSO_INTENT_KEY`). Multi-tenant picker. Bind → `/app/account`. |
| `forgot-password.tsx` | `/forgot-password` | `forgotPasswordRequest`. No session. |
| `reset-password.tsx` | `/reset-password` | `?token=` (≥20 chars). `resetPasswordRequest`. |
| `verify-email.tsx` | `/verify-email` | `?code=` or form. `verifyEmailRequest` then `/join` or `/app`. |
| `join-org.tsx` | `/join` | Session required. `lookupOrgByEmail` → `joinSuggestedOrg` / `joinDefaultOrg`. Has tenant → `/app`. Anon → `/login`. |
| `transfer.tsx` | `/transfer` | Session required. Password + optional invite. `transferOrg`. |
| `app/` | `/app/*` | Member workspace — see `app/AGENTS.md`. |

## CONVENTIONS

- No TanStack Query. No `QueryClient`. No page-level `fetch()` — go through `@/lib/auth-api` (or `useAuth` for session).
- Auth chrome: `@/components/auth/auth-shell`. Login/register own highlight arrays; others use `@/lib/auth-highlights`.
- SSO buttons: `@/components/auth/sso-buttons` (login/register only).
- Landing chrome: `SiteHeader` + `SiteFooter`. Auth pages do **not** use site chrome.
- Named exports (`LoginPage`, `JoinOrgPage`, …). Routes live in `src/routes/index.tsx`.
- Join/transfer are **not** under `ProtectedRoute`; they self-gate on `useAuth` status.
- Role copy on landing must match `engine/agent_templates/` when it claims product facts (landing shows **12 of 22** roles). Edit `components/sections/agents.tsx`, not this folder.
- `useAuth` rejects `platform_admin` — member funnel only.

## ANTI-PATTERNS

- Putting `/app` screens in this folder.
- Adding `QueryClient` or importing workspace/plaza/okr/control APIs here.
- Inventing IM connectors engine lacks (channel truth: `components/sections/AGENTS.md`).
- Calling `/api/admin/*` or creating `platform_admin` from register.
- Moving join/transfer under `/app`.
