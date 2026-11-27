# web-a/src/routes

Admin path table + RBAC gates. Parent: `../../AGENTS.md`. Screens: `../pages/AGENTS.md`.

## OVERVIEW

`AppRouter` is the path table. Guards wrap and read context; `TransferReviewRoute` uses `useTransferReviewAccess` without a tenant lookup.

| Path | Gate | Screen |
|------|------|--------|
| `/login` `/forgot-password` `/reset-password` | public | auth pages (no shell) |
| `/` | admin | overview |
| `/settings` | admin; the force-pw destination | profile, SMTP for genesis, transfer policy for PA, password, session |
| `/account` | admin | legacy redirect to `/settings` |
| `/users` `/users/:userId` | admin (PA + OA) | users list, user detail |
| `/models` | admin (PA + OA) | llm-models |
| `/tools` | admin (PA + OA) | `tools.tsx`; PA `?company=`, OA fixed own company |
| `/notifications` | admin (PA + OA) | current-user Inbox; unread badge in nav |
| `/activity` | admin (PA + OA) | bounded recent administrator activity; platform filters vs own organization |
| `/invitations` | `InvitationsRoute`: `canManageInvitations` (`org_admin`, not PA, has tenant) | invitations form + history |
| `/agent-deletion-requests` `/agent-deletion-requests/:id` | `AgentDeletionReviewRoute`: `canReviewAgentDeletions` (active `org_admin`, not PA, has tenant) | deletion queue, detail + approval |
| `/clawbook` | `ClawbookRoute`: `canViewClawbook` (active `org_admin`, not PA, has tenant) | read-only org posts, lazy comments |
| `/payments` | `TransferReviewRoute`: active PA; one "Payments" nav entry (`paymentReviewOnly`) | `payments.tsx`: `?tab=transfers` (default), `capacity`, `admissions`, `settings`; admissions/settings require the active genesis PA. Payment settings is a separate tab. |
| `/transfer-payments` `/capacity-payments` | same gates as their details | redirect (replace) to `/payments` and `/payments?tab=capacity` |
| `/transfer-payments/:id` | `TransferReviewRoute`: active PA only | payment detail + decision |
| `/capacity-payments/:orderId` | `CapacityReviewRoute`: `canReviewCapacityPayments` (active PA); no nav entry | extra-accounts payment detail with evidence and decision; engine notifications link here |
| `/capacity-orders/:orderId` | `CapacityOrderRoute`: `canViewCapacityOrders` (active non-PA org admin with a tenant, or active PA read-only); no nav entry | payment page: amount, bank recipient with copy buttons, evidence upload, cancel, status views |
| `/admission-approvals` `/admission-approvals/:id` | `AdmissionReviewRoute`: `canReviewAdmissions` (`org_admin` + `is_genesis`) | admission queue, detail + decision |
| `/organization-admissions` | `OrganizationAdmissionReviewRoute`: `canReviewOrganizationAdmissions` (active genesis `platform_admin`) | redirect (replace) to `/payments?tab=admissions`; distinct from the genesis OA member-admission gate |
| `/broadcast` | `BroadcastRoute`: `canSendBroadcast` (`org_admin`, not PA) | broadcast |
| `/companies` `/companies/:companyId` | `PlatformAdminRoute` | companies, company detail + domains + deletion |
| `/companies/:companyId/runtimes` | `PlatformAdminRoute`; no separate nav entry | company runtime inventory |
| `/search-engine` | `PlatformAdminRoute` | search-engine |
| `/openclaw-updates` | `PlatformAdminRoute` | openclaw-updates |
| `/upgrade-signals` | `PlatformAdminRoute` | upgrade-signals |
| `*` (inside shell) | none | `Navigate` → `/` |

Every row except public ones sits under `ProtectedRoute`; while `mustChangePassword`, everything but `/settings` redirects to `/settings`.
Analytics is `?tab=analytics` on `/search-engine`; payment tabs are query values on `/payments`. `/settings` is the separate account/organization settings route.

**Nesting:** public routes bare → `ProtectedRoute` → `SessionShell` (lazy: `AdminShell` inside the session `QueryClientProvider`) → `RouteErrorBoundary` (`../components/route-error-boundary.tsx`: a page that throws or fails to download leaves the shell and nav mounted; resets on every location change; Try again re-imports a failed page without a reload) → optional gate route (`AgentDeletionReviewRoute`, `ClawbookRoute`, `TransferReviewRoute`, `CapacityReviewRoute`, `CapacityOrderRoute`, `AdmissionReviewRoute`, `OrganizationAdmissionReviewRoute`, `BroadcastRoute`, `InvitationsRoute`, `PlatformAdminRoute`). A failed gate `Navigate`s to `/` (replace).

## WHERE TO LOOK

| File | Role |
|------|------|
| `index.tsx` | `AppRouter`, routed pages loaded through `retryingLazy` (`../lib/retrying-import.ts`) under one `Suspense`, `RouteErrorBoundary` as the pathless layout route directly inside `SessionShell`, `SessionShell` and `TransferReviewRoute` loaded lazily, plus `BroadcastRoute`, `InvitationsRoute`, `AdmissionReviewRoute`, `OrganizationAdmissionReviewRoute`, `AgentDeletionReviewRoute`, `ClawbookRoute`, `CapacityReviewRoute`, `CapacityOrderRoute`. |
| `session-shell.tsx` | `SessionShell`: `AdminShell` wrapped in `SessionQueryProvider` (`../hooks/AGENTS.md`). Lazy, so Query, motion and the shell's hooks stay out of the entry chunk; nothing above it (login, password pages, `ProtectedRoute`) may use Query. |
| `protected.tsx` | Auth + `isAdmin`. Loading spinner. Unauth / non-admin → `/login`. Force-pw allowlist: `/settings` (and `/*` under it) until cleared; else `/settings`. |
| `platform-admin.tsx` | `isPlatformAdminUser` else `/`. Wraps `/companies*`, `/search-engine`, `/openclaw-updates` and `/upgrade-signals`; their nav items use `platformAdminOnly`. |
| `transfer-review.tsx` | `TransferReviewRoute`: `useTransferReviewAccess().canReview ? Outlet : /`. |

Nav chrome: `../components/layout/admin-shell.tsx` (per-gate nav flags, force-pw locks links). Add routes here first; a new nav destination needs an entry with the same predicate (see `../components/layout/AGENTS.md`). Detail routes can reuse an existing destination.

## CONVENTIONS

- JWT `maraclaw-admin-token` via `useAuth`. Guards read context, not `localStorage`.
- Force-change is `mustChangePassword` from `useAuth`, enforced by `ProtectedRoute`. A 403 never logs out; only an authenticated 401 expires the session (`../lib/AGENTS.md`).
- Non-admin after login → `/login`. Members never see this app (`web-e`).
- New routed screen: page + route here; add nav only for a navigation destination. Capacity-order and company-runtime details need no separate nav entry. Query-string tabs stay query-string.
- Catch-all is inside the shell, not a second public `*`.

## ANTI-PATTERNS

- `/app` or other member routes (that is `web-e`).
- Implementing APIs or `fetch` in this folder; capability lookups belong in `hooks/`.
- A `/search-engine/analytics` path (keep `?tab=analytics`).
- Metrics or Enterprise-settings routes until engine + page exist. Those two Overview cards are teasers (no `href`); Companies and Users cards link.
- Treating `VITE_AUTH_BYPASS` as implemented; it is not.
