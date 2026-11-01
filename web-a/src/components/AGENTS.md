# web-a/src/components

**Generated:** 2026-11-01
**Commit:** a591022e
**Branch:** 2026November

Parent: `../../AGENTS.md`. Screens: `../pages/AGENTS.md`. HTTP: `../lib/AGENTS.md`. Nav/shell detail: `layout/AGENTS.md`; primitives: `ui/AGENTS.md`.

## OVERVIEW

Shared admin primitives, domain widgets/forms, shells, navigation and brand. Keep screen-specific composition in `pages/`; extract here when reused.

## WHERE TO LOOK

| Family | Files / responsibility |
|--------|------------------------|
| Primitives | `ui/{badge,button,card,input,label,password-field,select,separator}.tsx` |
| Shells | `layout/admin-shell.tsx`: protected nav + `Outlet`; `layout/auth-shell.tsx`: forgot/reset chrome (login has its own) |
| Navigation | `layout/nav-icon.tsx`: active/inactive assets; `layout/section-rail.tsx`: route links or local selection |
| Notifications | `layout/admin-shell.tsx`: shared Notifications nav tile and unread badge |
| Company UI | `companies/create-company-form.tsx`: company + genesis OA; `companies/company-status-icon.tsx`: active/disabled state |
| User rows | `users/user-card.tsx`: linked row, toggle action and slots; page owns role/toggle policy |
| Agent transfer | `users/transfer-agents.tsx`: `TransferAgents` dialog rendered in a `UserCard` slot by `pages/users-departments.tsx` |
| Brand | `brand/maraclaw-logo.tsx`: `MaraClawLogo`; `brand/maraclaw-mark.tsx`: public SVG mark |
| Theme | `theme-toggle.tsx`: `useTheme` control; provider/storage live in `../hooks/use-theme.tsx` |

## PRIMITIVES AND BRAND

- `ui/` is the existing `web-e` subset, not a full shadcn catalog: no accordion, sheet or textarea. Check local exports before using a primitive.
- Preserve Button `destructive`, Badge `success|warning|destructive`, and PasswordField `hideLeadingIcon`; merge classes through `cn` from `@/lib/utils`.
- Package `components.json` maps components/UI aliases here; primitives use the shared CSS variables in `src/index.css` (CSS-first Tailwind v4).
- Match the `web-e` brand source: `MaraClawLogo`, `public/maraclaw-mark.svg` and warm OKLCH tokens. Do not fork the mark.

## SHELLS AND NAVIGATION

- `AdminShell` owns desktop sidebar/mobile navigation, `platformAdminOnly` visibility and force-password-change link locking; route guards remain in `routes/`.
- Preserve the `bg-card/70` sidebar and footer email + theme + Sign out. Shared chrome changes belong here, not in individual pages.
- `AuthShell` accepts title, description and footer; forgot/reset use it, but login deliberately owns its chrome.
- `NavIcon` uses `public/nav-icons/` active/inactive SVG pairs and reduced-motion handling. `SectionRail` uses `NavLink` when `to` exists, otherwise selection buttons; pages own query-string tabs.

## DOMAIN FORMS AND ROWS

- `CreateCompanyForm` uses RHF + Zod, `noValidate`, `useId()`, lowercase trimmed emails and minimum-six-character passwords; preserve its 409/403 error handling.
- Its requests go through `companies-api.ts`; retain the `useSessionCurrent()` post-await fence before errors, reset or `onCreated`. The caller owns query invalidation.
- `UserCard` takes `canToggle`, pending state and callbacks; keep the overlay link `z-10` and actions `z-20`, plus the `web-a:users-scroll` handoff.
- `TransferAgents` renders nothing unless `canTransferMemberAgents(user, source)` (active non-PA `org_admin`, same tenant, end-user source), no `mustChangePassword`, and `source.tenant_id`. Its dialog uses Radix Dialog directly, not a `ui/` primitive.
- Transfer submit stays disabled until source detail and every-cursor recipient list are loaded and settled; source is re-checked against the viewer and selected agents against the fresh non-system list. Recipients must pass `isAgentTransferTarget`; errors map through `agentLifecycleError`. Mutations come from `hooks/agent-transfer-options.ts` via `useSessionMutation`.
- Keep company form/status UI in `companies/`, user rows and transfer in `users/`, shared shells in `layout/`, marks in `brand/`, and reusable primitives in `ui/`.
