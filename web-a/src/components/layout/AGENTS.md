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
2. Each gated entry has a flag matching the route guard in `routes/` and the predicate in `src/lib/types/auth-guards.ts`
   (or `transfer-review-access.ts`). Change entry, guard and predicate together.
3. `platformAdminOnly` marks `/companies`, `/search-engine`, `/openclaw-updates`, `/upgrade-signals`.
   `isPlatformAdminUser` accepts the platform flag or the `platform_admin` role.
4. `broadcastOnly` (`/broadcast`) uses `canSendBroadcast`: role `org_admin`, not a platform admin.
   `invitationsOnly` (`/invitations`) uses `canManageInvitations`: the same plus a non-null `tenant_id`.
5. `admissionReviewOnly` (`/admission-approvals`) uses `canReviewAdmissions`: role `org_admin` and `is_genesis === true`.
   `agentDeletionReviewOnly` (`/agent-deletion-requests`) uses `canReviewAgentDeletions`:
   active `org_admin`, not a platform admin, with a `tenant_id`.
   `clawbookOnly` (`/clawbook`, directly after Users) uses `canViewClawbook`, the same gate.
6. `paymentReviewOnly` (`/payments`, label "Payments") uses `useTransferReviewAccess().canReview`, called by both `NavItems`.
   Only active platform admins pass, using the canonical platform flag or role, without a tenant lookup.
   It is the only payment nav entry: `/payments` has `transfers` (default), `capacity`, `admissions` and `settings` tabs.
   Transfers/capacity allow active PAs; admissions/settings are visible only to the active genesis PA through their respective predicates.
   Settings mounts separately from the capacity queue. The guarded `/organization-admissions` alias redirects to `/payments?tab=admissions`; it does not use the genesis OA Admissions nav gate.
   Detail pages such as `/capacity-orders/:orderId` and `/companies/:companyId/runtimes` have no separate nav entry.
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
    `NavIcon` renders one `<img>` without framer-motion; a state change remounts it with a `motion-safe:` CSS enter,
    so reduced motion and first paint show the plain image.

## Gotchas

- `/search-engine` pathname prefixes and `/payments` remove the main area's usual padding.
- `aliases` on a nav item (Payments: `/transfer-payments`, `/capacity-payments`) lets detail routes outside its prefix highlight the entry, set `aria-current="page"` and set the document title through the shared `matchesNavItem`. Shell entries use `Link` with that shared current state because `NavLink` drops `aria-current` outside its own prefix.
- Mobile nav scrolls the alias-aware current link into view on pathname changes, not query changes; do not reject detail aliases by checking the link's pathname prefix.
- `AuthShell.className` styles the centered content wrapper, not the page root.
