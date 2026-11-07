# Layout components

Scope: console navigation, notification entry point and shared screen chrome.

## Files

- `admin-shell.tsx`: console frame, navigation visibility and routed content.
- `auth-shell.tsx`: titled auth-screen chrome with optional description and footer.
- `nav-icon.tsx`: active/inactive navigation icon rendering.
- `section-rail.tsx`: routed or locally selected secondary navigation.

## Invariants

1. Desktop and compact mobile navigation share `NavItems` and `navItems`.
   Notifications follows Overview for both admin roles. Visibility filters run before password-lock rendering.
   `tests/admin-navigation.test.ts` regex-parses `navItems` from source; keep flat object literals.
2. Each gated entry has a flag matching the route guard in `routes/` and the predicate in `src/lib/types/auth.ts`
   (or `transfer-review-access.ts`). Change entry, guard and predicate together.
3. `platformAdminOnly` marks `/companies`, `/search-engine`, `/openclaw-updates`.
   `isPlatformAdminUser` accepts the platform flag or the `platform_admin` role.
4. `broadcastOnly` (`/broadcast`) uses `canSendBroadcast`: role `org_admin`, not a platform admin.
   `invitationsOnly` (`/invitations`) uses `canManageInvitations`: the same plus a non-null `tenant_id`.
5. `admissionReviewOnly` (`/admission-approvals`) uses `canReviewAdmissions`: role `org_admin` and `is_genesis === true`.
   `agentDeletionReviewOnly` (`/agent-deletion-requests`) uses `canReviewAgentDeletions`:
   active `org_admin`, not a platform admin, with a `tenant_id`.
6. `paymentReviewOnly` (`/transfer-payments`) uses `useTransferReviewAccess().canReview`, called by both `NavItems`.
   Active platform admins pass. An org admin needs an active genesis account whose `/api/tenants/me`
   is its own active default end-user organization; the entry stays hidden while that lookup is pending or failed.
7. Under `mustChangePassword`, surviving nav entries other than `/settings`
   render non-link spans with `aria-disabled="true"`.
   Notifications uses this same locked span presentation.
8. The Notifications nav tile links to `/notifications`; positive counts show
   a badge capped at `99+` with the full unread count in its accessible label.
   Locked mode hides the badge.
9. `SectionRail` items with `to` use `NavLink` and router-derived active state.
   Others call `onSelect(item.id)` through `SectionRailButton`; without `onSelect`, omit them.
   `itemProps` applies only to the button branch.
10. Preserve reduced-motion branches: `AuthShell` suppresses entrance motion;
    `NavIcon` substitutes a plain image for its animated state transition.

## Gotchas

- `/search-engine` pathname prefixes remove the main area's usual padding.
- Mobile nav scrolls the active link into view on pathname changes, not query changes.
- `AuthShell.className` styles the centered content wrapper, not the page root.
