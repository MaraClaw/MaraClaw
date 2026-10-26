# hooks/ - providers, query options and workspace scopes

Scope: auth/theme context lifetimes, session-fenced query adapters, notification/update option factories.

## Files

### Providers

- `auth-provider.tsx`: `AuthProvider`, external auth-store subscription and context action wiring.
- `theme-provider.tsx`: `ThemeProvider`, stored/system theme resolution and document updates.

### Query-option factories

- `notification-options.ts`: `notificationKeys`, `notificationListOptions`, `notificationUnreadOptions`, `notificationReadOptions`, `notificationsReadAllOptions`; notification queries and read mutations.
- `openclaw-update-options.ts`: `agentUpdateKey`, `upgradeMutationKey`, `agentUpdateOptions`, `upgradeOpenClawOptions`; update polling, upgrade mutation and related invalidation.

### Workspace scope utilities and hooks

- `session-query-scope.ts`: `RetiredWorkspaceError`, `createSessionQueryScope`, `SessionQueryScope`, `fenceMutationOptions`; captured owner/client execution and callback fences.
- `session-workspace.ts`: `WorkspaceSessionContext`, `useWorkspaceSession`, `useQuery`, `useMutation`, `useInfiniteQuery`; scoped TanStack adapters.

### Context consumers and composed hooks

- `use-auth.ts`: `AuthContextValue`, `AuthContext`, `useAuth`; typed auth context and required-provider access.
- `use-openclaw-update.ts`: `useOpenClawUpdate`; update query, scoped upgrade-mutation count and work gate.
- `use-theme.ts`: `Theme`, `ResolvedTheme`, `ThemeContext`, `useTheme`; theme contract and required-provider access.

## Invariants

- `AuthProvider.login` validates its owner before and after the request; returns token/multi-tenant results without installing them.
  `applySession` remains a separate context action; unexpected login responses throw `ApiError(500)`.
- `useAuth`, `useTheme` and `useWorkspaceSession` throw when their respective context is absent.
  Option factories return options, not hook results; workspace adapters bind them to `scope.client`.
- `scope.run` asserts currency before and after awaited work; `scope.effect` skips retired-owner callbacks.
  `fenceMutationOptions` wraps mutation/onMutate execution and success/error/settled callbacks separately.
- Workspace mutation wrappers also fence `mutate` and `mutateAsync`.
  Infinite-query wrappers fence manual next-page, previous-page and refetch actions as well as the query function.
- Notification lists start at offset 0, request 50 rows and advance only after a full 50-row page.
  Only `notificationUnreadOptions` sets the 30,000 ms polling interval; category belongs in both query keys.
- Notification single, all, selected-read, archive and restore factories use mutation scope `notifications-read`.
  Success cancels the `notifications` prefix before invalidating every category with `refetchType: 'all'`.
- `use-notification-selection.ts` scopes selected loaded rows to the current owner/view/category and caps selection at 200. Selection identity fences late read completions; context identity fences load-more completions.
- Update polling: 1,500 ms for `isActiveUpdate(run)`, otherwise 30,000 ms, including background polling.
  Query enabled only for a nonempty agent ID; stale time 0, always refetch on mount and refetch on focus.
  Newly observed terminal runs refresh agent/list/detail queries; upgrade success installs its run before invalidation.
- Theme toggling chooses explicit light/dark opposite the resolved theme; it does not cycle back to system.
  Invalid stored values resolve to the system preference; media-listener cleanup removes the change handler.

## Gotchas

- Scope subscription cleanup only unsubscribes; it does not clear a still-current client during StrictMode rehearsal.
  Actual owner retirement cancels queries and clears the captured client.
- `useOpenClawUpdate` feeds cached data to its gate only after a post-mount fetch or while the cached run is active.
