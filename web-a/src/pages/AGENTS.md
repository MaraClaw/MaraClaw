# web-a/src/pages

Screen composition roots. Parent: `../../AGENTS.md`. HTTP: `../lib/AGENTS.md`.

## OVERVIEW

Each page owns Query/mutations + layout. Extract a component only when reused.

## WHERE TO LOOK

| Screen | File | Notes |
|--------|------|-------|
| Login | `login.tsx` | Custom chrome (not `AuthShell`). Multi-tenant re-calls `login` with `tenant_id`. |
| Forgot / reset | `forgot-password.tsx`, `reset-password.tsx` | Use `AuthShell`. |
| Overview | `overview.tsx` | PA gets `SearchAnalyticsSnapshot`. |
| Settings | `settings.tsx`, `settings-password.tsx`, `tenant-email-*.tsx`, `transfer-policy-*` | Profile, eligible organization settings, Change password, Session/Sign out. Password changes retain owner fences and validate the replacement token through `refreshReplacement`. `TenantEmailSettings` only for `tenantEmailScope` (active genesis admin with a tenant); `TransferPolicySettings` only for platform admins. Both link to `#change-password` instead of loading while `mustChangePassword`. |
| Companies | `companies.tsx` | Debounced server `?q=`. 403 + `tenant_id` → `getTenant` fallback. |
| Company domains | `company-detail.tsx`, `company-agent-sections.tsx`, `company-deletion.tsx` | Loads full `listCompanies()` then `.find`. `CompanyAgentSections`: a switch per server-ordered section (on = visible to members in `web-e`); a toggle PUTs the whole hidden list, disables switches while pending and keeps the last visible section on. Deletion: platform admins only, hidden for system and default end-user orgs; preview first, then type the company name; counts are advisory. |
| Users | `users.tsx`, `user-detail.tsx`, `users-pagination.ts`, `users-roster-export.ts` | URL `?q=` `?company=`. Company users use server search and opaque-cursor pages (`useUsersPagination`, key `['admin-users', tenant, 'page', search, generation, revision]`, `gcTime: 0`); never filter them client-side. Platform admins are a separate unpaged list, filtered client-side on name/email, shown to PAs only. PAs must pick a company (defaults to own, else first); OAs use their tenant. Scroll key `web-a:users-scroll`. |
| Roster export | `users-roster-export.ts` | `useUsersRosterExport` downloads the server CSV for the current tenant + search. Changing either, the session owner or unmounting retires it; late results never download or set state. |
| Departments | `users-departments.tsx`, `users-department-{card,merge,memberships,queries,mutations}` | Org departments live on Users, not a route; end users only. `TenantUsers` owns create, cards (rename, delete with preview, merge) and per-member multi-department checkboxes (replace list), and slots `TransferAgents` into each `UserCard`. Merge: pick destination → preview returns `confirmation_token` → checkbox confirms that token → POST. Changing destination or a 409 `department_merge_preview_changed` clears confirmation and refetches. Success drops the source from cache, moves non-overlapping assignments, then invalidates directory, `['admin-users', tenant]` and affected `['admin-user', id]`. |
| Invitations | `invitations.tsx`, `invitations-{form,history,row}.tsx` | Own-tenant OA only; children are keyed by tenant so a tenant change remounts them. Page size 20; revoke and send invalidate the tenant's history. |
| Notifications | `notifications.tsx` | Cursor-stack paging over the `active` and `archived` views; selection (max 200) clears on page change and after a successful bulk action; any successful mutation returns to page one; destinations pass `safeNotificationLink`. |
| Activity | `admin-audit.tsx` | `adminAuditPageOptions` cursor pages with a refresh counter. PA scope adds tenant/actor filters; OA sends action, target type and time window only. |
| Admissions | `admission-approvals.tsx`, `admission-approval-detail.tsx`, `admission-labels.ts` | Genesis org admins; cursor stack by status. Decisions through `useAdmissionDecision`. Approve needs the confirmation box, cleared after each attempt; `organization-admissions.tsx` uses the same gate. |
| Agent deletions | `agent-deletion-requests.tsx`, `agent-deletion-request-detail.tsx` | Opening a detail records the review; a list view is not one. Approve needs `isFetchedAfterMount` and not fetching, plus the confirmation checkbox; the hook re-checks the cached detail. |
| Transfer payments | `transfer-payments.tsx`, `transfer-payment-detail.tsx`, `transfer-payment-labels.ts` | Cursor-stack paging per view (`pending`/`recent`) over the server-filtered queue; a view change returns to page one. Approval only creates a destination admission; deciding needs a pending paid `payment_review` and the payment-checked box. `canOpenTransferAdmission` gates the admission link. |
| Broadcast | `broadcast.tsx`, `broadcast-email-progress.tsx` | The reviewed snapshot is a `BroadcastSubmission` (draft + `Idempotency-Key`); retry after an error resends the same key, a changed draft or success mints a new one. Never regenerate the key on retry. Success view and progress key off the returned `broadcast_id`. Email status polls every 5s only while queued or claimed rows remain; cancel can race workers, so refetch status afterwards. |
| OpenClaw updates | `openclaw-updates.tsx` | Saved request lives in per-admin `sessionStorage`; corrupt saved data can be discarded. |
| Models | `llm-models.tsx`, `chatgpt-subscription-card.tsx` | Org + platform admin. PA uses `?company=`. Writes `/api/enterprise/llm-models`. Set primary (`/set-default`), secondary (`/set-secondary`), and fallback (`/set-fallback`). Grok retains device-code start/poll. ChatGPT opens browser sign-in, then POSTs a pasted full callback URL; read-only status runs once after ambiguous completion, never polls. Tenant-keyed card and session fences discard late UI effects. Callback input clears on submit and never enters mutation variables, query keys, storage, logs or toasts. |
| Search keys | `search-engine.tsx` | Keys/Analytics icon rail. `?tab=analytics` mounts analytics. Leaving the tab drops `tab`/`company`/`range`. |
| Analytics | `search-engine-analytics.tsx` | Not a route. `?company=` `?range=`. Dual system+scoped summary is intentional. |
| Tools | `tools.tsx`, `tools-catalog.tsx`, `tools-editor.tsx`, `tools-config-editor.tsx`, `tools-delete.tsx` | Live catalog/MCP create/metadata and status/delete confirmation. PA `?company=`, OA fixed own tenant. Ownership labels use source/`tenant_id`, never selected scope; required server `capabilities` gate actions (no client ownership policy). Creation/config still require a concrete company; built-in delete stays hidden. `tools-catalog-state.ts` tests presentation gates; `tools-forms.ts` / `tools-config.ts` own payload and secret boundaries. Metadata validates only exposed fields and omits config; explicit config is full replacement with secret re-entry. Scope-keyed editors + `use-tools-action.ts` fence stale UI effects; successful current-session writes invalidate all Tools scopes even after an editor unmounts. Pending writes disable Cancel; successful deletion focuses the catalog heading. |

Largest files: `llm-models.tsx` (live Models screen), `search-engine-analytics.tsx`, `login.tsx`. Split a new concern out rather than growing them.

## CONVENTIONS

- Chrome: `mx-auto max-w-{2xl|3xl|5xl|6xl} flex-col gap-6`; title `font-display text-2xl`.
- Query keys: `['admin-companies']`, `['admin-users', tenant, ...]`, `['admin-departments', tenant]`, `['admin-platform-admins']`, `['admin-user', id]`, `['admin-tavily-keys']`, `['admin-llm-models', tenant]`, `['admin-llm-providers']`, `['email-domains', id]`, `['admin-search-analytics-*', scope]`. Review, notification and invitation keys live in `../hooks/*-options.ts`.
- `['admin-companies']`, `['admin-users']` and `['admin-user']` are shared prefixes: user toggles, department changes, agent transfers and deletion approvals invalidate them. Keep those key shapes stable; after company/user mutations invalidate them.
- Errors: `error instanceof ApiError ? error.message : 'Failed to load …'`. Toasts on mutate. Special-case 409 (dup) / 403 (role).
- Forms: RHF + Zod, `noValidate`, `useId()`. Emails `.trim().toLowerCase()`. Passwords min 6.
- List cards: overlay `<Link className="absolute inset-0 z-10">`; actions `relative z-20`.
- `canToggle` (users): never self / `is_genesis`. Members: PA or same-tenant OA. PA rows: genesis PA only. OA rows: genesis OA same tenant.

## ANTI-PATTERNS

- A new `/search-engine/analytics` route (keep the query-string tab).
- Inventing analytics metrics (engine is truth).
- Breaking shared `['admin-companies']` invalidation.
