# Hooks and session infrastructure

Scope: query-option factories, context providers, session ownership and consumer hooks.

## Files

### Query-option factories
- `admin-audit-options.ts`: validated audit queries (recent list and cursor page).
- `admission-options.ts`: admission query keys, reads and decision cache updates.
- `organization-admission-options.ts`: new-organization status/offset reads, guarded decisions and credentials-email retry.
- `notification-options.ts`: notification keys, reads, read/archive/restore cache updates.
- `agent-deletion-options.ts`: deletion queue/detail reads and the approval mutation.
- `agent-transfer-options.ts`: transfer source/recipient reads and the ownership-transfer mutation.
- `transfer-review-options.ts`: payment queue/detail reads and the decision mutation.
- `capacity-options.ts`: `['admin-capacity', ...]` reads (overview, orders, order, review queue/detail, settings) and the create, evidence, cancel, decision and settings mutations.
- `invitation-options.ts`: tenant-keyed invitation history, send and revoke.
- `broadcast-options.ts`: broadcast email-status polling.
- `resource-pressure-options.ts`: parsed-filter summary and cursor-page episode reads (`['admin-resource-pressure', ...]`, `retry: false`).
- `organization-plan-options.ts`: the public plan catalog read (`['organization-plans']`, `retry: false`) behind the create-company form's plan summaries.
- `clawbook-options.ts`: Clawbook post pages and per-post comment pages (infinite, cursor `null` first, no retry).
- `company-options.ts`: company reads and mutations; `companyRuntimeOptions` keys runtime pages by company and cursor, forwards the signal and disables retries. Picker reads live under `['admin-companies', 'options', ...]`: `companyOptionsBrowseOptions` (infinite, load more by `next_cursor`), `companyOptionsSearchOptions(q)` (one ranked page keyed by the trimmed query, skipped when empty, no load more) and `companyOptionByIdOptions(id)` (`getTenant`, 404 is `null`, a non-UUID is never sent; `isCompanyId`).

### Providers
- `use-auth.tsx`: auth context, session-scoped query provider and auth consumers.
- `use-theme.tsx`: theme preference, resolution, context and DOM application.
- `use-translation.tsx`: `TranslationProvider` and `useTranslation`, persisted locale and localized number/date/distance formatting.

### Session ownership helpers
- `session-mutation.ts`, `session-query-client.ts`: mutation ownership/lifecycle fencing and session query-client creation/retirement.

### Consumer hooks
- `use-admissions.ts`, `use-agent-deletions.ts`: role-gated reads and session-owned decisions.
- `use-clawbook.ts`: read-only Clawbook posts and comments, gated by `canViewClawbook` and no lock; comments load only while open.
- `use-organization-admissions.ts`: active genesis PA reads, decisions and credentials-email retry; password-change locks disable access.
- `use-notifications.ts`: gated notification reads and session-owned read/archive mutations.
- `use-transfer-reviews.ts`: `useTransferReviewAccess` capability plus gated payment reads and decisions.
- `use-capacity.ts`: `useCapacityAccess` (`canBuy`, `canView`, `canReview`, `canManageSettings`, all false under a password-change lock) plus the gated reads and session-owned mutations.
- `use-session-mutation.ts`: React mutation binding to the current auth owner.
- `use-debounced-value.ts`: `useDebouncedValue(value, delayMs)` for search inputs (Companies list, company picker).

## Invariants

1. `AuthProvider` memoizes query scope by `snapshot.owner` and keys `QueryClientProvider` by `snapshot.owner.generation`.
   `useSessionCurrent` captures that owner for later current-session checks; retain both boundaries.
2. Session query defaults: `staleTime: 30_000`, `retry: 1`, no window-focus refetch. Retirement cancels and clears; effect cleanup only cancels.
3. `ownMutationOptions` wraps execution with `runOwned`, guards `onMutate` through `assertCurrent`, and fences success/error/settled with `isCurrent`.
4. `adminAuditOptions` parses scope before keying `['admin-audit', parsed]`; `adminAuditPageOptions`
   strips `cursor` and keys `['admin-audit-page', scope, cursor, refresh]`. Both forward the signal and set `retry: false`.
5. Member admission reads require `canReviewAdmissions(user)` (genesis OA) and no lock; null detail IDs use `skipToken`, reads poll every 30 seconds.
6. Notification reads require `isAdmin` and no password-change lock; list and unread count poll every 30 seconds.
   List keys include the `active|archived` view. Read, read-all, archive, restore and selected variants share
   scope `admin-notifications-read`. Single-read decrements unread count only if an active cached page held the item unread and unarchived; other mutations cancel/invalidate the family.
7. Member admission decisions disable retry and use a per-admission scope; success updates detail, removes the pending row and invalidates admissions plus notifications.
   An `ApiError` with status `409` cancels and invalidates admission queries.
8. Deletion reads require `canReviewAgentDeletions(user)` and no lock; detail uses `staleTime: 0`, `gcTime: 0`, no retry.
   The approval `mutationFn` throws a local 403 `detail_view_required` unless the cached detail is `success` and idle
   and `canApproveAgentDeletion` holds (pending, `can_approve`, `has_viewed`, not `has_approved`); it never POSTs on a stale view.
   Success stores the detail, then invalidates deletion queries, notifications, `['admin-users']`, `['admin-user']`
   and `['admin-companies']`. A 403 or 409 cancels and invalidates deletion queries.
9. `useTransferReviewAccess().canReview` requires no password-change lock and `canReviewTransferPayments`
   (active platform admin only), without a tenant capability lookup.
   Queue and detail are enabled only by `canReview` and poll every 30 seconds. `transferDecisionOptions` throws a local 403
   when not allowed. Queue pages key on `['admin-transfer-payments','list',view,cursor]`. Success cancels polls, sets detail, removes the decided request from every cached `pending` page, replaces it in `recent` pages and invalidates transfers plus notifications;
   only a 409 invalidates on error.
10. Agent transfer reads (`gcTime: 0`, `staleTime: 0`, no retry) fetch the source detail and every recipient page. Success cancels and invalidates transfers, users, user, companies, deletion queries and notifications; a 403 or 409 invalidates only transfers. No automatic retry.
11. Invitation reads need a non-null tenant and key on `[tenant, page]`; send and revoke invalidate `invitationKeys.tenant`.
12. Capacity reads use `useCapacityAccess`; company-scoped reads omit `tenant_id` for OAs and send the selected company for PAs.
    Order lists key on the view and the page boundary (`capacityKeys.orders(tenant, limit, cursor)`, `capacityKeys.reviewList(status, cursor)`; `null` is the first page); the cursor is the server's opaque `next_cursor`, never parsed.
    Every capacity mutation guards before requesting with local 403 `CapacityGuardError` (translation key via `capacityGuardKey`) and disables retries.
    Evidence/cancel/decision share `capacity-order-{id}`; create uses `capacity-order-create`, settings uses `capacity-payment-settings`.
    Order mutations store the returned order (decision uses the review-detail key); success cancels/invalidates capacity and invalidates notifications. Their 404/409/403 handler cancels/invalidates only capacity.
    Settings success stores settings at `capacityKeys.settings` and refreshes the same families; settings has no conflict/error handler.
13. `ThemeProvider` persists `maraclaw-admin-theme`: `light|dark|system`, invalid/missing becomes `system`; resolution is `light|dark`.
    DOM application updates root dark class, color scheme, theme dataset and theme-color meta.
    Theme and language storage go through `../lib/preference-storage.ts`: when the browser refuses storage, both providers still render and keep changes in memory.
14. `TranslationProvider` persists `en|vi` in `maraclaw-admin-language` (invalid/missing becomes `en`) and updates document `lang`.
    Vietnamese strings are a lazy chunk (`../lib/translations.ts`, imported only by `loadVietnamese()` in `../lib/i18n.ts`; other files use type-only imports). The provider renders nothing while a stored `vi` locale loads, falls back to English on a failed load (stored choice kept), and `setLocale('vi')` loads before switching. Non-React callers and tests `await loadVietnamese()` before relying on `vi` lookups.
    Typed messages and the client-owned translation boundary live in `../lib/i18n.ts`; API detail and user content stay outside that boundary.

## Gotchas

- Query `enabled` does not guard mutations. Deletion approval, transfer-payment decisions, every capacity mutation and organization-admission decisions/email retries have their own pre-request guards.
- Retiring client work does not undo server writes already accepted.
- `toggleTheme` chooses explicit light/dark from the resolved theme; it exits system mode.
- `useAuth`, `useTheme` and `useTranslation` throw outside their corresponding providers.
