# web-a/src/routes

Admin path table + RBAC gates. Parent: `../../AGENTS.md`. Screens: `../pages/AGENTS.md`.

## OVERVIEW

`AppRouter` is the path table. Guards wrap and read context; only `TransferReviewRoute` triggers a fetch, through `useTransferReviewAccess`.

| Path | Gate | Screen |
|------|------|--------|
| `/login` `/forgot-password` `/reset-password` | public | auth pages (no shell) |
| `/` | admin | overview |
| `/account` `/settings` | admin; the only force-pw allowlist | account, settings (SMTP for genesis, transfer policy for PA) |
| `/users` `/users/:userId` | admin (PA + OA) | users list, user detail |
| `/models` | admin (PA + OA) | llm-models |
| `/tools` | admin (PA + OA) | `tools.tsx`; PA `?company=`, OA fixed own company |
| `/notifications` | admin (PA + OA) | current-user Inbox; unread badge in nav |
| `/activity` | admin (PA + OA) | bounded recent administrator activity; platform filters vs own organization |
| `/invitations` | `InvitationsRoute`: `canManageInvitations` (`org_admin`, not PA, has tenant) | invitations form + history |
| `/agent-deletion-requests` `/agent-deletion-requests/:id` | `AgentDeletionReviewRoute`: `canReviewAgentDeletions` (active `org_admin`, not PA, has tenant) | deletion queue, detail + approval |
| `/transfer-payments` `/transfer-payments/:id` | `TransferReviewRoute`: PA, or genesis OA of the default end-user org (tenant lookup) | payment queue, detail + decision |
| `/admission-approvals` `/admission-approvals/:id` | `AdmissionReviewRoute`: `canReviewAdmissions` (`org_admin` + `is_genesis`) | admission queue, detail + decision |
| `/broadcast` | `BroadcastRoute`: `canSendBroadcast` (`org_admin`, not PA) | broadcast |
| `/companies` `/companies/:companyId` | `PlatformAdminRoute` | companies, company detail + domains + deletion |
| `/search-engine` | `PlatformAdminRoute` | search-engine |
| `/openclaw-updates` | `PlatformAdminRoute` | openclaw-updates |
| `*` (inside shell) | none | `Navigate` → `/` |

Every row except public ones sits under `ProtectedRoute`; while `mustChangePassword`, everything but `/account` and `/settings` redirects to `/account`.
Analytics is `?tab=analytics` on `/search-engine`, not a route.

**Nesting:** public routes bare → `ProtectedRoute` → `AdminShell` → optional gate route (`AgentDeletionReviewRoute`, `TransferReviewRoute`, `AdmissionReviewRoute`, `BroadcastRoute`, `InvitationsRoute`, `PlatformAdminRoute`). A failed gate `Navigate`s to `/` (replace).

## WHERE TO LOOK

| File | Role |
|------|------|
| `index.tsx` | `AppRouter`, all pages lazy under one `Suspense`, plus the `BroadcastRoute`, `InvitationsRoute`, `AdmissionReviewRoute`, `AgentDeletionReviewRoute` guards. |
| `protected.tsx` | Auth + `isAdmin`. Loading spinner. Unauth / non-admin → `/login`. Force-pw allowlist: `/account` and `/settings` (and `/*` under them) until cleared; else `/account`. |
| `platform-admin.tsx` | `isPlatformAdminUser` else `/`. Wraps `/companies*`, `/search-engine` and `/openclaw-updates`; their nav items use `platformAdminOnly`. |
| `transfer-review.tsx` | `TransferReviewRoute`: waits on `useTransferReviewAccess().isPending`, shows a retry card on lookup error (stays locked), else `canReview ? Outlet : /`. |

Nav chrome: `../components/layout/admin-shell.tsx` (per-gate nav flags, force-pw locks links). Add the route here first, then the nav entry with the same predicate (see `../components/layout/AGENTS.md`).

## CONVENTIONS

- JWT `maraclaw-admin-token` via `useAuth`. Guards read context, not `localStorage`.
- Force-change is `mustChangePassword` from `useAuth`, enforced by `ProtectedRoute`. A 403 never logs out; only an authenticated 401 expires the session (`../lib/AGENTS.md`).
- Non-admin after login → `/login`. Members never see this app (`web-e`).
- New live screen: page + route here + nav. Query-string tabs stay query-string.
- Catch-all is inside the shell, not a second public `*`.

## ANTI-PATTERNS

- `/app` or other member routes (that is `web-e`).
- Implementing APIs or `fetch` in this folder; capability lookups belong in `hooks/`.
- A `/search-engine/analytics` path (keep `?tab=analytics`).
- Metrics or Enterprise-settings routes until engine + page exist. Those two Overview cards are teasers (no `href`); Companies and Users cards link.
- Treating `VITE_AUTH_BYPASS` as implemented — it is not.
