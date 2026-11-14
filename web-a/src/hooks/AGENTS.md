# Hooks and session infrastructure

Scope: query-option factories, context providers, session ownership and consumer hooks.

## Files

### Query-option factories
- `admin-audit-options.ts`: validated audit queries (recent list and cursor page).
- `admission-options.ts`: admission query keys, reads and decision cache updates.
- `notification-options.ts`: notification keys, reads, read/archive/restore cache updates.
- `agent-deletion-options.ts`: deletion queue/detail reads and the approval mutation.
- `agent-transfer-options.ts`: transfer source/recipient reads and the ownership-transfer mutation.
- `transfer-review-options.ts`: payment queue/detail reads and the decision mutation.
- `invitation-options.ts`: tenant-keyed invitation history, send and revoke.
- `broadcast-options.ts`: broadcast email-status polling.
- `resource-pressure-options.ts`: parsed-filter summary and cursor-page episode reads (`['admin-resource-pressure', ...]`, `retry: false`).

### Providers
- `use-auth.tsx`: auth context, session-scoped query provider and auth consumers.
- `use-theme.tsx`: theme preference, resolution, context and DOM application.

### Session ownership helpers
- `session-mutation.ts`: mutation ownership and lifecycle callback fencing.
- `session-query-client.ts`: session query-client creation and retirement.

### Consumer hooks
- `use-admissions.ts`, `use-agent-deletions.ts`: role-gated reads and session-owned decisions.
- `use-notifications.ts`: gated notification reads and session-owned read/archive mutations.
- `use-transfer-reviews.ts`: `useTransferReviewAccess` capability plus gated payment reads and decisions.
- `use-session-mutation.ts`: React mutation binding to the current auth owner.

## Invariants

1. `AuthProvider` memoizes the query scope by `snapshot.owner` and keys
   `QueryClientProvider` by `snapshot.owner.generation`; retain both boundaries.
   `useSessionCurrent` captures that owner for later current-session checks.
2. Session query defaults: `staleTime: 30_000`, `retry: 1`, no window-focus refetch.
   Retirement cancels queries and clears the client; effect cleanup only cancels.
3. `ownMutationOptions` wraps execution with `runOwned` and guards `onMutate`
   through `assertCurrent`; success, error and settled callbacks check `isCurrent`.
4. `adminAuditOptions` parses scope before keying `['admin-audit', parsed]`; `adminAuditPageOptions`
   strips `cursor` from the parsed scope and keys `['admin-audit-page', scope, cursor, refresh]`.
   Both forward the query signal and set `retry: false`.
5. Admission reads require `canReviewAdmissions(user)` and no password-change lock.
   In `web-a/src/lib/types/auth.ts`, that helper requires `org_admin` plus `is_genesis === true`.
   Null detail IDs use `skipToken`; admission read factories poll every 30 seconds.
6. Notification reads require `isAdmin` and no password-change lock; list and unread count poll every 30 seconds.
   List keys include the `active|archived` view. Read, read-all, archive, restore and selected variants share
   scope `admin-notifications-read`. Single-read decrements the cached unread count only when an active cached
   page held that item unread and unarchived; other mutations cancel and invalidate the family.
7. Admission decisions disable retry and use a per-admission mutation scope.
   Success updates detail, removes the pending row and invalidates admissions plus notifications.
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
10. Agent transfer reads (`gcTime: 0`, `staleTime: 0`, no retry) fetch the source detail and every recipient page.
    Success cancels and invalidates transfers, users, user, companies, deletion queries and notifications;
    a 403 or 409 invalidates only the transfer family. No automatic retry.
11. Invitation reads need a non-null tenant and key on `[tenant, page]`; send and revoke invalidate `invitationKeys.tenant`.
12. `ThemeProvider` reads/writes `maraclaw-admin-theme`; missing/invalid values resolve via `system`.
    Accepted preferences: `light`, `dark`, `system`; resolved values: `light`, `dark`.
    DOM application updates the root dark class, color scheme, theme dataset and theme-color meta.

## Gotchas

- Read-query `enabled` gates do not guard mutation hooks; only deletion approval and payment decisions carry their own pre-request guards.
- Retiring client work does not undo server writes already accepted.
- `toggleTheme` chooses explicit light/dark from the resolved theme; it exits system mode.
- `useAuth` and `useTheme` throw outside their corresponding providers.
