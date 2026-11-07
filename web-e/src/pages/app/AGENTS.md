# pages/app/ — member workspace screens

**Generated:** 2026-11-01 · Parent: `web-e/AGENTS.md`

## OVERVIEW

Member `/app/*` screens. Gate: ProtectedRoute → AppShell (owns QueryClient, staleTime 30s) → OnboardingGate → pages. Settings, admissions and transfer detail are AppShell children outside OnboardingGate, and Inbox has its own admission branch (package and layout guides). Profile and password remain accessible before onboarding or a required password change; admissions remain subject to the password-change gate. Legacy Account URLs redirect to Settings.

## WHERE TO LOOK

| Route | Page | API |
|-------|------|-----|
| `/app` | → `agents` | — |
| `/app/onboarding` | `onboarding.tsx` | `hooks/onboarding-options` mutation (start, create personal assistant, complete); status via the workspace-api facade |
| `/app/account` | → `/app/settings` | Legacy redirect |
| `/app/settings` | `settings.tsx` | auth-api (profile, password, SSO bind, tenants/switch); `settings-timezone-card.tsx` for the timezone preference |
| `/app/notifications?category=admissions`, `/app/admissions/:id` | `admissions.tsx`, `admission-detail.tsx` | Inbox composes applicant history with All statuses default (50 per page, status filter, reset on an invalid cursor); `/app/admissions` redirects here. Detail adds withdrawal and explicit approved workspace entry via auth-api switch/replacement. `transfer-requests.tsx` adds cursor-paged transfer requests above the list; detail return links target Inbox Admissions |
| `/app/admissions/transfers/:id` | `transfer-detail.tsx` | company-transfers-api; saved fee/instructions, withdrawal while in payment review, link to the destination admission. An approved destination admission enters the workspace through `useJoinedWorkspace` |
| `/app/notifications` | `notifications.tsx` | URL-backed Category selector composes Admissions separately from notification data. Notification categories retain `hooks/notification-options` through the session-scoped query hooks (cursor-paged list, unread count) and mutation factories that take `scope.client` (read, unread, archive, restore, selected variants); `use-notification-selection`; `lib/notifications-api` only for wire categories, views and types. Remaps `/clawbook…` → `/app/clawbook…` |
| `/app/updates/:noticeId` | `openclaw-update-detail.tsx` | openclaw-updates-api; route uses `openclawUpdateRoute` from `lib/openclaw-update-policy.ts` |
| `/app/clawbook` | `clawbook.tsx` | clawbook-api; `org_admin` can delete |
| `/app/okr` | `okr.tsx` | okr-api; `org_admin` settings/outreach |
| `/app/directory` | `directory.tsx` | People/Org Departments icon rail; `?tab=` `?q=`. `org-departments-api` serves org department name cards with org-admin inline rename; `directory-api` serves people from `/api/org/users`. Provider sync and the Synced view are retired |
| `/app/agents` | `agents-list.tsx` | workspace-api |
| `/app/agents/new` | `agent-new.tsx` | workspace-api; Private defaults to `user`, Company-wide to `company`, Department to `custom` + one `department_ids` entry in the creation POST. Options use `org-departments-api`; creator manages, department members use. Always OpenClaw |
| `/app/agents/:agentId` | `AgentLayout` | GET `/api/agents/:id`; outlet `{ agent: AgentOut }` — most tabs must **not** refetch |
| `…/chat` (+ `:sessionId`) | `agent-chat.tsx` | `useChatHistory` cursor-paged history (`chat-history-query`) with `HistoryControl` and `useChatScroll`, plus `connectAgentChat` WS; the session list is an infinite query keyed `['sessions', agentId, scope]` (`all` for the creator, else `mine`) behind `SessionList`; a URL-opened session absent from loaded pages resolves via `getSession`; uploads via workspace-api |
| `…/activity` | `agent-activity.tsx` | activity-api cursor pages with an action-type filter (infinite query, no cache retention). Manage-only: the tab is hidden without `manage` access and the page refuses `use`; a 403 evicts cached summaries and downgrades the cached access level |
| `…/files` `skills` `tools` `tasks` `schedules` `channels` `relationships` `permissions` `settings` | `agent-*.tsx` | workspace-api facade; `settings` also uses lifecycle, setup and expiry hooks |
| `…/control` | `agent-control.tsx` | control-api lock/screenshot/click/type/drag/keys; env `browser`\|`computer`\|`code`; its own `['sessions', agentId, 'control', 'mine']` first page, filtered by `isWritableSession`, never chat's cache |
| `…/credentials` | `agent-credentials.tsx` | control-api vault + gogcli; **manage** only |
| `…/pages` | `agent-pages.tsx` | control-api publish `.html` → `/p/{id}` |
| `…/playwright` | `agent-playwright.tsx` | workspace-api `agentbay_browser_*` + `.crabbox` |

## CONVENTIONS

- New tenant screen: route in `routes/index.tsx` + nav in `app-shell.tsx` + `nav-icon` + `public/nav-icons/*.svg` + new `lib/*-api.ts` if not agent-scoped.
- New agent tab: `agent-layout` `tabs[]` (`to` on `SectionRail`) + nested route + `public/nav-icons/{name}.svg` and `{name}-inactive.svg`; `useOutletContext<{ agent: AgentOut }>()`.
- New control RPC: `control-api.ts`, not workspace-api.
- Chat events: `ChatInbound` + `onEvent` switch in `agent-chat.tsx`.
- Chat upload submissions and history carry `attachments: [{path, name}]`; retain those references when replacing pending rows with authoritative history. Cards/renderers live in `components/chat/`, authenticated preview I/O in `lib/attachment-preview-api.ts`. Native downloads still mint fresh tickets; the Files tab is unchanged. Images, PDF and local DOCX/XLSX/PPTX previews are supported; legacy Office remains download-only.
- Chat history is cursor-paged: the latest window is bridged to the previous newest entry, earlier pages prepend on demand, and `mergeServerHistory` keeps pending and live rows. Composer `@skill` mentions come from the installed-skills query and submit workspace-relative skill paths (`lib/chat/skill-mentions.ts`). Component and library details: `components/chat/` and `lib/chat/` guides.
- Authz: (1) `agent.access_level` `use` vs `manage` (vault, tools, publish, activity, start/stop); (2) `user.role` / `creator_id` (`org_admin` Clawbook/OKR; creator delete/approvals). Do not surface LLM provider, model names, or model pickers here — that stays in `web-a` `/models`. Company Grok and ChatGPT subscriptions are connected in web-a; members inherit them through the company pool. If chat fails with no model, the engine message points admins at Models.
- Permissions tab department grants: `components/agent-department-access.tsx` + `lib/agent-departments-api.ts` (query `['permission-departments', agentId]`). Custom list only; level is `use`, not `manage`. Saving Private or Whole company clears them. Admins create the departments on web-a Users.
- Directory org departments: `components/org-departments.tsx` + `org-department-card.tsx`, cache key remains `['local-departments']`. Members read `/api/departments/options`; only `org_admin` can rename here. Rename refreshes department names and the `permissions` / `permission-departments` prefixes. Creation and membership stay in web-a Users; never use the privileged `GET /api/departments` for members.
- Forms: most tabs are useState + sonner, not RHF. Motion wrappers are marketing-only.
- Query keys invented in pages (`['agent', id]`, `['sessions', agent.id, scope]`, `['clawbook', …]`) — keep consistent when sharing. Chat and Take Control deliberately do not share a sessions key (`lib/chat-sessions-query.ts`).

## ANTI-PATTERNS

- Refetching the agent on a tab that already has outlet context.
- Growing `workspace-api.ts` for Clawbook/OKR/directory/control — add `lib/*-api.ts`.
- Tenant screen only in this folder — also wire route, rail, and icons.
- Agent tab without `tabs[]` + nested route.
- RHF or Reveal/Stagger on agent tabs.
