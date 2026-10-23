# Layout components

Scope: console navigation, notification entry point and shared screen chrome.

## Files

- `admin-shell.tsx`: console frame, navigation visibility and routed content.
- `auth-shell.tsx`: titled auth-screen chrome with optional description and footer.
- `nav-icon.tsx`: active/inactive navigation icon rendering.
- `notification-bell.tsx`: unread notification link and locked presentation.
- `section-rail.tsx`: routed or locally selected secondary navigation.

## Invariants

1. Desktop and compact mobile navigation share `NavItems` and `navItems`.
   Visibility filters run before password-lock rendering.
2. `platformAdminOnly` marks `/companies`, `/search-engine`, `/openclaw-updates`.
   `isPlatformAdminUser` accepts the platform flag or the `platform_admin` role.
3. `/broadcast` uses `canSendBroadcast` from `web-a/src/lib/types/auth.ts`.
   Requires role `org_admin` and a false `isPlatformAdminUser` result.
4. `/admission-approvals` uses `canReviewAdmissions` from that same file.
   Requires role `org_admin` and `is_genesis === true`.
5. Under `mustChangePassword`, surviving nav entries other than `/account`
   and `/settings` render non-link spans with `aria-disabled="true"`.
   Both notification-bell placements receive the same password-lock state.
6. Unlocked `NotificationBell` links to `/notifications`; positive counts show
   a badge capped at `99+`. Locked mode disables the button and hides the badge.
7. `SectionRail` items with `to` use `NavLink` and router-derived active state.
   Others call `onSelect(item.id)` through `SectionRailButton`; without `onSelect`, omit them.
   `itemProps` applies only to the button branch.
8. Preserve reduced-motion branches: `AuthShell` suppresses entrance motion;
   `NavIcon` substitutes a plain image for its animated state transition.

## Gotchas

- `/search-engine` pathname prefixes remove the main area's usual padding.
- Mobile nav scrolls the active link into view on pathname changes, not query changes.
- `AuthShell.className` styles the centered content wrapper, not the page root.
- Compact bells use a tooltip; `expanded` bells return the link without one.
