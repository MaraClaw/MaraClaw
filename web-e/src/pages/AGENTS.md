# pages/ — public + org-funnel

**Generated:** 2026-11-01 · Parent: `web-e/AGENTS.md`

This folder is **public + org-funnel only**. Workspace screens live in `pages/app/` (nested `AGENTS.md`). Do not add `/app` pages here.

## OVERVIEW

Landing + member auth + join/transfer **outside** `/app`. Nine files here; `app/` is a different surface.

## WHERE TO LOOK

| File | Route | Role |
|------|-------|------|
| `landing.tsx` | `/` | Composer only: Hero→Features→Agents→HowItWorks→Integrations→Enterprise→Faq→Cta. Hashes stay on `/`. Copy in `@/components/sections/*`. |
| `login.tsx` | `/login` | RHF+Zod. `useAuth().login`. Multi-tenant picker; unverified → `/verify-email`. `needs_org_confirm` → `/join`, except a `/transfer#code=` continuation is kept. |
| `register.tsx` | `/register` | `fetchRegistrationConfig` (invite gate), `checkDuplicate`, `registerRequest`. Optional or required `invitation_code`; a returned admission request routes to `/app/admissions/:id`. Cannot mint `platform_admin`. |
| `sso-callback.tsx` | `/sso/callback` | `oauthCallback` or `bindProvider` (`SSO_INTENT_KEY`) with the server-issued state kept in sessionStorage (never the URL's); a missing or differing stored state fails before any request. Multi-tenant picker. Bind → `/app/settings`. |
| `forgot-password.tsx` | `/forgot-password` | `forgotPasswordRequest`. No session. |
| `reset-password.tsx` | `/reset-password` | `?token=` (≥20 chars). `resetPasswordRequest`. |
| `verify-email.tsx` | `/verify-email` | `?code=` or form. `verifyEmailRequest` then `/join` or `/app`. |
| `join-org.tsx` | `/join` | Session required. Pending admission check first → `/app/notifications?category=admissions`. Invitation code: `useInvitationPreview` shows the destination, then `joinInvitation` accepts it; otherwise `joinDefaultOrg`. Pending result renders `AdmissionPending`; approved or immediate results go through `useJoinedWorkspace`. Has tenant → `/app`. Anon → `/login`. |
| `transfer.tsx` | `/transfer` | Session required, tenant required (else `/join`). Invitation preview, then password. `useTransferFunnel` loads the quote (default-organization sources only); `TransferChoice` forces paid or free (free needs destructive consent). Outcomes: immediate join, pending/approved admission, or payment review linking to `/app/admissions/transfers/:id`. |
| `app/` | `/app/*` | Member workspace — see `app/AGENTS.md`. |

## CONVENTIONS

- No TanStack Query. No `QueryClient`. No page-level `fetch()` — go through `@/lib/auth-api` (or `useAuth` for session).
- Auth chrome: `@/components/auth/auth-shell`. Login/register own highlight arrays; others use `@/lib/auth-highlights`.
- SSO buttons: `@/components/auth/sso-buttons` (login/register only).
- Landing chrome: `SiteHeader` + `SiteFooter`. Auth pages do **not** use site chrome.
- Named exports (`LoginPage`, `JoinOrgPage`, …). Routes live in `src/routes/index.tsx`.
- Join/transfer are **not** under `ProtectedRoute`; they self-gate on `useAuth` status and send `must_change_password` sessions to `/app/settings`.
- Invitation codes arrive via `#code=` or router state `invitationCode`; `useInvitationLink` reads them once (256 chars max) and clears the URL. Both pages remount their form per session owner, and only the first generation keeps the initial code.
- Shared funnel UI is `@/components/auth/invitation-code` (validate and preview) and `transfer-choice` (paid/free dialog); `admission-pending` links pending requests.
- Role copy on landing must match `engine/agent_templates/` when it claims product facts (landing shows **12 of 22** roles). Edit `components/sections/agents.tsx`, not this folder.
- Auth responses use member-specific contracts; the server enforces member-audience eligibility.

## ANTI-PATTERNS

- Putting `/app` screens in this folder.
- Adding `QueryClient` or importing workspace/clawbook/okr/control APIs here.
- Inventing IM connectors engine lacks (channel truth: `components/sections/AGENTS.md`).
- Calling `/api/admin/*` or creating `platform_admin` from register.
- Moving join/transfer under `/app`.
- Treating `lookupOrgByEmail` / `joinSuggestedOrg` in `auth-api.ts` as the `/join` flow: no page calls them (only a test does); joining uses the invitation or default-org calls.
