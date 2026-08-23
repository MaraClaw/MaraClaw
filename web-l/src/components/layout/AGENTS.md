# layout/ — workspace + marketing chrome

**Generated:** 2026-08-23 · Parent: `web-l/AGENTS.md`

## OVERVIEW

Workspace + marketing chrome.

## WHERE TO LOOK

| File | Role |
|------|------|
| `app-shell.tsx` | **Only** `QueryClientProvider` in the SPA (`staleTime` 30s, `retry` 1, no focus refetch). Workspace rail: Agents / Plaza / OKR / Directory / Inbox / Account / Settings. Desktop aside + mobile top strip. Inbox badge: `unreadNotificationCount` (30s poll). |
| `agent-layout.tsx` | Per-agent header + `SectionRail` tabs. `Outlet` context `{ agent }`. Start/stop only when `access_level === 'manage'`. |
| `section-rail.tsx` | Shared icon rail. Route `to` (agent tabs) or `onSelect` (directory `?tab=`). Smaller than the workspace rail — do not merge. |
| `onboarding-gate.tsx` | First-run: no agents + status not `completed` + not skipped → `/app/onboarding`. Skip key `maraclaw-onboarding-skipped` (`wasOnboardingSkipped` in workspace-api). |
| `site-header.tsx` / `site-footer.tsx` | Marketing chrome only. Header `navItems` omit `#enterprise`; footer has it. Known hash mismatch — do not fix one side only. |
| `nav-icon.tsx` | Rail icons from `public/nav-icons/{name}.svg` + `{name}-inactive.svg`. Add both when extending `NavIconName`. |

## CONVENTIONS

- QueryClient lives here. Do **not** put one in `App.tsx` or `main.tsx`.
- Marketing / auth pages must **not** import `AppShell`.
- Brand via `@/components/brand/maraclaw-logo` — do not fork.
- New workspace dest: `nav[]` in `app-shell` + `NavIconName` + both SVGs. Live route first.
- New agent dest: `tabs[]` in `agent-layout` (not workspace `nav[]`) + nested route.

## ANTI-PATTERNS

- A second `QueryClient` / extra `QueryClientProvider`.
- Inventing rail items for engine features that are not live `/app` routes.
- Moving agent tabs into the workspace rail (or workspace items into `SectionRail`).
- Syncing header `#enterprise` (or dropping footer) without intent.
- Importing `SiteHeader` / `SiteFooter` inside `/app`.
