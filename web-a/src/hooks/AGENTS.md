# Hooks and session infrastructure

Scope: query-option factories, context providers, session ownership and consumer hooks.

## Files

### Query-option factories
- `admin-audit-options.ts`: validated audit query construction.
- `admission-options.ts`: admission query keys, reads and decision cache updates.
- `notification-options.ts`: notification query keys, reads and read-state cache updates.

### Providers
- `use-auth.tsx`: auth context, session-scoped query provider and auth consumers.
- `use-theme.tsx`: theme preference, resolution, context and DOM application.

### Session ownership helpers
- `session-mutation.ts`: mutation ownership and lifecycle callback fencing.
- `session-query-client.ts`: session query-client creation and retirement.

### Consumer hooks
- `use-admissions.ts`: gated admission reads and session-owned decisions.
- `use-notifications.ts`: gated notification reads and session-owned read mutations.
- `use-session-mutation.ts`: React mutation binding to the current auth owner.

## Invariants

1. `AuthProvider` memoizes the query scope by `snapshot.owner` and keys
   `QueryClientProvider` by `snapshot.owner.generation`; retain both boundaries.
   `useSessionCurrent` captures that owner for later current-session checks.
2. Session query defaults: `staleTime: 30_000`, `retry: 1`, no window-focus refetch.
   Retirement cancels queries and clears the client; effect cleanup only cancels.
3. `ownMutationOptions` wraps execution with `runOwned` and guards `onMutate`
   through `assertCurrent`; success, error and settled callbacks check `isCurrent`.
4. `adminAuditOptions` parses scope before using it in `['admin-audit', parsed]`.
   It forwards the query signal and sets `retry: false`.
5. Admission reads require `canReviewAdmissions(user)` and no password-change lock.
   In `web-a/src/lib/types/auth.ts`, that helper requires `org_admin` plus `is_genesis === true`.
   Null detail IDs use `skipToken`; admission read factories poll every 30 seconds.
6. Notification reads require `isAdmin` and no password-change lock; poll every 30 seconds.
   Single/all-read mutations share scope `admin-notifications-read` and invalidate their family.
   Single-read decrements cached unread count only when a cached list contained that unread item.
7. Admission decisions disable retry and use a per-admission mutation scope.
   Success updates detail, removes the pending row and invalidates admissions plus notifications.
   An `ApiError` with status `409` cancels and invalidates admission queries.
8. `ThemeProvider` reads/writes `maraclaw-admin-theme`; missing/invalid values resolve via `system`.
   Accepted preferences: `light`, `dark`, `system`; resolved values: `light`, `dark`.
   DOM application updates the root dark class, color scheme, theme dataset and theme-color meta.

## Gotchas

- Read-query `enabled` gates do not guard calls to the mutation hooks.
- Retiring client work does not undo server writes already accepted.
- `toggleTheme` chooses explicit light/dark from the resolved theme; it exits system mode.
- `useAuth` and `useTheme` throw outside their corresponding providers.
