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
| Primitives | `ui/AGENTS.md`: styled controls, back/refresh controls and select-options helper |
| Shells | `layout/admin-shell.tsx`: protected nav + `Outlet`; `layout/auth-shell.tsx`: forgot/reset chrome (login has its own) |
| Navigation | `layout/nav-icon.tsx`: active/inactive assets; `layout/section-rail.tsx`: route links or local selection |
| Notifications | `layout/admin-shell.tsx`: shared Notifications nav tile and unread badge |
| Company UI | `companies/create-company-form.tsx`: company + plan + genesis OA; `companies/company-plan-badge.tsx`: Pro/Max/Ultra badge (every company has a tier); `companies/company-status-icon.tsx`: active/disabled state; `companies/company-picker.tsx`: `useCompanyPicker` + `CompanyPicker`, the one bounded company selector (search, load more, selection resolved by id and pinned when off the loaded pages) for Users, Models, Tools, Analytics and Upgrade signals |
| User rows | `users/user-card.tsx`: linked row, toggle action and slots; page owns role/toggle policy |
| Agent transfer | `users/transfer-agents.tsx`: `TransferAgents` dialog rendered in a `UserCard` slot by `pages/users-departments.tsx` |
| Agent daily messages | `users/agent-daily-messages.tsx`: `AgentDailyMessages` usage/limit and reset-time display; guarded reset dialog on user detail |
| Capacity | `capacity/`: shared by the payment page, the review detail and Users. `capacity-labels.ts` (status/kind/tier labels, `capacityActionClass`), `capacity-status-badge.tsx`, `capacity-order-details.tsx` (the order's quote, period, amounts and evidence summary), `copy-field.tsx` (value + Copy button + status text; `copyValue` for the raw amount), `capacity-payment-actions.tsx` (`CapacityEvidenceForm`: Finish with a client-checked file; `CapacityCancelOrder`: inline confirmation; both show translated copy for the evidence/cancel refusals through `lib/capacity-errors.ts`) |
| Payment evidence | `payment-evidence-preview.tsx`: `PaymentEvidencePreview` takes `load(signal)` (a Bearer bytes loader such as `fetchCapacityEvidence` / `fetchOrganizationAdmissionEvidence`), a stable `loadKey` (a new key remounts and refetches; a new `load` function alone does not) and `{filename, content_type}`. The bytes become an object URL, revoked on unmount; images show inline, PDF as "Open PDF" (new tab; the blob page inherits the production CSP, whose `object-src 'none'` can stop a PDF viewer in some browsers) plus "Download PDF" (same blob URL, `download` = evidence filename) so the file is always reachable; other types render nothing; `BinaryLimitError` and 404 have their own messages; Retry remounts it. Used by the capacity order and review pages and by the organization admission review. |
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

- `CreateCompanyForm` uses RHF + `createCompanySchema` (`lib/company-form-schema.ts`), `noValidate`, `useId()`, lowercase trimmed emails and minimum-six-character passwords; preserve its 409/403 error handling. It validates `onTouched` and on submit: Create company stays actionable for invalid drafts so `handleSubmit` exposes errors and focuses the first invalid control. Disable it only while submitting to prevent duplicates. Company name and org admin email errors use stable ids and `aria-describedby`, like the phone/password errors.
- Its required Phone number shares the first row with Company name from `sm` (stacked below): a native `Select` (aria-label "Country code", `phoneCountryOptions(locale)`, default Vietnam +84, restored by `reset()`) beside an `Input type="tel"`. Submit sends `toE164(country, number)` as `phone` (compact E.164).
- Its required Plan is a fieldset of three native radios (`Controller`, no default, "Choose a plan" when missing) after Company name; ids come from `OrganizationPlanIdValues`, names from `capacityTierLabels`, and each shows price, included accounts and agents from the public catalog (`organizationPlansOptions`). If the catalog fails, the radios keep only their names and creation is not blocked. `createCompany` always sends `tier`.
- Its requests go through `companies-api.ts`; retain the `useSessionCurrent()` post-await fence before errors, reset or `onCreated`. The caller owns query invalidation.
- `UserCard` takes `canToggle`, pending state and callbacks; keep the overlay link `z-10` and actions `z-20`, plus the `web-a:users-scroll` handoff.
- `TransferAgents` renders nothing unless `canTransferMemberAgents(user, source)` (active non-PA `org_admin`, same tenant, end-user source), no `mustChangePassword`, and `source.tenant_id`. Its dialog uses Radix Dialog directly, not a `ui/` primitive.
- Transfer submit stays disabled until source detail and every-cursor recipient list are loaded and settled; source is re-checked against the viewer and selected agents against the fresh non-system list. Recipients must pass `isAgentTransferTarget`; errors map through `agentLifecycleError`. Mutations come from `hooks/agent-transfer-options.ts` via `useSessionMutation`.
- `AgentDailyMessages` renders only when the agent has `daily_messages`. Its reset dialog requires `canResetAgentDailyMessages(user, owner)` and no password-change lock; the trigger is disabled below the limit. Pending reset blocks dialog close and repeat submission; retain its error/success announcements and session-owned mutation.
- Keep company form/status UI in `companies/`, user rows, transfer and daily messages in `users/`, shared shells in `layout/`, marks in `brand/`, and reusable primitives in `ui/`.
