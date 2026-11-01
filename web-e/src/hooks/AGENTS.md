# hooks/ - providers, query options and workspace scopes

Scope: auth/theme context lifetimes, session-fenced query adapters, query-option factories, and the invitation, transfer, chat-history and agent-lifecycle hooks built on them.

## Files

### Providers

- `auth-provider.tsx`: `AuthProvider`, external auth-store subscription and context action wiring.
- `theme-provider.tsx`: `ThemeProvider`, stored/system theme resolution and document updates.

### Query-option factories

- `notification-options.ts`: `notificationKeys`, `notificationListOptions`, `notificationUnreadOptions` and mutation options for read, read-all, archive and restore, plus selected read/unread/archive/restore variants.
- `openclaw-update-options.ts`: `agentUpdateKey`, `upgradeMutationKey`, `agentUpdateOptions`, `upgradeOpenClawOptions`; update polling, upgrade mutation and related invalidation.
- `agent-lifecycle-options.ts`, `agent-setup-options.ts`: lifecycle and setup keys, polling query options, optional-MCP and setup retry mutations.
- `agent-skills-options.ts`: `installedAgentSkillsOptions`; installed skills for chat mentions.
- `onboarding-options.ts`: `assistantOnboardingOptions`; start, create-assistant and complete as one scoped mutation.

### Workspace scope utilities and hooks

- `session-query-scope.ts`: `RetiredWorkspaceError`, `createSessionQueryScope`, `SessionQueryScope`, `fenceMutationOptions`; captured owner/client execution and callback fences.
- `session-workspace.ts`: `WorkspaceSessionContext`, `useWorkspaceSession`, `useQuery`, `useMutation`, `useInfiniteQuery`; scoped TanStack adapters.

### Context consumers and composed hooks

- `use-auth.ts`: `AuthContextValue`, `AuthContext`, `useAuth`; typed auth context and required-provider access.
- `use-admission-access.ts`: `useAdmissionAccess`; pending-admission query exposing `workspaceAllowed`.
- `use-invitation-link.ts`, `use-invitation-preview.ts`, `use-joined-workspace.ts`, `use-transfer-funnel.ts`: invitation code intake, preview, joined-workspace entry and transfer quote/choice/payment state.
- `use-chat-history.ts`, `use-chat-scroll.ts`: cursor-paged chat history with earlier-page loading, and scroll anchoring for it.
- `use-agent-expiry.ts`, `use-agent-lifecycle.ts`, `use-agent-setup.ts`: expiry timer, lifecycle status/MCP retry and setup status/retry.
- `use-notification-selection.ts`: selected loaded notification rows per owner/view/category.
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
- Notification lists start without a cursor, request 50 rows and advance only with the returned next cursor.
  Only `notificationUnreadOptions` sets the 30,000 ms polling interval; category belongs in both query keys.
- Every notification mutation factory, single or selected, uses mutation scope `notifications-read`.
  Success cancels the `notifications` prefix and trims cached traversals to their first page before invalidating every category with `refetchType: 'all'`.
- `use-notification-selection.ts` scopes selected loaded rows to the current owner/view/category and caps selection at 200. Selection identity fences late read completions; context identity fences load-more completions.
- `useAdmissionAccess` keys on `['company-admissions','workspace-access', tenantId]`, never retries, and refetches every 30,000 ms only while a pending admission exists. `workspaceAllowed` needs a successful `false`, so loading and error states do not open the workspace.
- `useInvitationLink` reads `#code=` (256 chars max) or router state `invitationCode` once, then replaces the URL to clear both. `useInvitationPreview` aborts on edit or unmount and drops completions from a retired owner.
- `useJoinedWorkspace`: a pending result is a no-op; an approved admission calls `switchTenant` for its target; an immediate join uses its own token. Then `refreshReplacement`, a success toast and `/app`.
- `useTransferFunnel` loads the quote under an abort controller and owner check. `submit` stages a selection when `explicit_choice_required`, otherwise executes. Execution is single-flight and blocked unless `source_eligible` with no held admission or payment; a payment-review result stores `payment`, an admission result stores `admission` then enters.
- `useChatHistory` reads the latest window and keeps merged pages under `chatHistoryKey`. `loadEarlier` is single-flight, aborts on target change, and a cursor that fails to advance surfaces as an error.
- `useChatScroll` anchors on the first visible `[data-chat-entry]` across prepends and sticks to the bottom within 80 px. Earlier-page loading arms at `scrollTop` 160 or less and re-arms after scrolling past it; scope changes reset it.
- Agent lifecycle polls every 1,500 ms while setup, start, upgrade (`pending`/`cutover`) or an optional MCP is active, otherwise 30,000 ms, in the background too. Setup polls 1,500 ms only while active. Both need `canManage` and an agent ID, use stale time 0 and never retry. `useAgentExpiry` chunks timers to 2,147,483,647 ms and invalidates agent and lifecycle queries at expiry.
- Update polling: 1,500 ms for `isActiveUpdate(run)`, otherwise 30,000 ms, including background polling.
  Query enabled only for a nonempty agent ID; stale time 0, always refetch on mount and refetch on focus.
  Newly observed terminal runs refresh agent/list/detail queries; upgrade success installs its run before invalidation.
- Theme toggling chooses explicit light/dark opposite the resolved theme; it does not cycle back to system.
  Invalid stored values resolve to the system preference; media-listener cleanup removes the change handler.

## Gotchas

- Scope subscription cleanup only unsubscribes; it does not clear a still-current client during StrictMode rehearsal.
  Actual owner retirement cancels queries and clears the captured client.
- `useOpenClawUpdate` feeds cached data to its gate only after a post-mount fetch or while the cached run is active.
