# pages/app/ — member workspace screens

**Generated:** 2026-08-16 · Parent: `web-l/AGENTS.md`

## OVERVIEW

Member `/app/*` screens (23). Gate: ProtectedRoute → AppShell (owns QueryClient, staleTime 30s) → OnboardingGate → pages.

## WHERE TO LOOK

| Route | Page | API |
|-------|------|-----|
| `/app` | → `agents` | — |
| `/app/onboarding` | `onboarding.tsx` | workspace-api |
| `/app/account` | `account.tsx` | auth-api (profile, password, SSO bind) |
| `/app/settings` | `settings.tsx` | auth-api tenants/switch |
| `/app/notifications` | `notifications.tsx` | workspace-api; remap `/plaza…` → `/app/plaza…` |
| `/app/plaza` | `plaza.tsx` | plaza-api; `org_admin` can delete |
| `/app/okr` | `okr.tsx` | okr-api; `org_admin` settings/outreach |
| `/app/directory` | `directory.tsx` | directory-api; People/Synced/Departments icon rail; `?tab=` `?q=` |
| `/app/agents` | `agents-list.tsx` | workspace-api |
| `/app/agents/new` | `agent-new.tsx` | workspace-api; default `permission_scope_type: user`; always OpenClaw |
| `/app/agents/:agentId` | `AgentLayout` | GET `/api/agents/:id`; outlet `{ agent: AgentOut }` — most tabs must **not** refetch |
| `…/chat` (+ `:sessionId`) | `agent-chat.tsx` | workspace-api REST history + `connectAgentChat` WS |
| `…/files` `skills` `tools` `tasks` `schedules` `channels` `relationships` `permissions` `settings` | `agent-*.tsx` | workspace-api |
| `…/control` | `agent-control.tsx` | control-api lock/screenshot/click/type/drag/keys; env `browser`\|`computer`\|`code` |
| `…/credentials` | `agent-credentials.tsx` | control-api vault + gogcli; **manage** only |
| `…/pages` | `agent-pages.tsx` | control-api publish `.html` → `/p/{id}` |
| `…/playwright` | `agent-playwright.tsx` | workspace-api `agentbay_browser_*` + `.crabbox` |

## CONVENTIONS

- New tenant screen: route in `routes/index.tsx` + nav in `app-shell.tsx` + `nav-icon` + `public/nav-icons/*.svg` + new `lib/*-api.ts` if not agent-scoped.
- New agent tab: `agent-layout` `tabs[]` (`to` on `SectionRail`) + nested route + `public/nav-icons/{name}.svg` and `{name}-inactive.svg`; `useOutletContext<{ agent: AgentOut }>()`.
- New control RPC: `control-api.ts`, not workspace-api.
- Chat events: `ChatInbound` + `onEvent` switch in `agent-chat.tsx`.
- Authz: (1) `agent.access_level` `use` vs `manage` (vault, tools, publish, start/stop); (2) `user.role` / `creator_id` (`org_admin` Plaza/OKR; creator delete/approvals). Do not surface LLM provider, model names, or model pickers here — that stays in `web-a` `/models`.
- Forms: most tabs are useState + sonner, not RHF. Motion wrappers are marketing-only.
- Query keys invented in pages (`['agent', id]`, `['sessions', agent.id]`, `['plaza', …]`) — keep consistent when sharing.

## ANTI-PATTERNS

- Refetching the agent on a tab that already has outlet context.
- Growing `workspace-api.ts` for Plaza/OKR/directory/control — add `lib/*-api.ts`.
- Tenant screen only in this folder — also wire route, rail, and icons.
- Agent tab without `tabs[]` + nested route.
- RHF or Reveal/Stagger on agent tabs.
