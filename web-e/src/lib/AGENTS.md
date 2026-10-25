# lib/ - engine clients

**Generated:** 2026-10-23 · Parent: `web-e/AGENTS.md`

## OVERVIEW

Engine HTTP/WS/storage layer. Pages never `fetch()`. New domain → new `*-api.ts`.

## STRUCTURE

| File | Owns |
|------|------|
| `api.ts` | `getApiBaseUrl` / `apiUrl` / `wsUrl`. Empty `VITE_API_BASE_URL` = same-origin. Rejects hostname `0.0.0.0`. |
| `http.ts` | Only fetch wrapper: `apiRequest`, `apiFormRequest`, `apiBinaryRequest`, `ApiError`, `formatApiDetail`, `userFacingRequestError`. Bearer unless `token` overridden. Owner fences cover headers/body awaits; current-owner 401 expires that session. JSON 204 → `undefined`. Binary previews stream with a 20 MiB cap, no-store and no redirects. |
| `auth-storage.ts` | localStorage `maraclaw-enduser-token` |
| `auth-api.ts` | `/api/member/auth/*` for member login/register/SSO/current user; shared `/api/auth/*` for password/tickets/tenant selection and `/api/tenants/*` for join/switch. Public calls pass `token: null`. |
| `types/auth.ts` | Generated member DTO adapters: `UserOut`, `TokenResponse`, `RegisterInitResponse`, `MultiTenantResponse`, `isTokenResponse`. Audience eligibility is enforced by the server. |
| `company-admissions-api.ts`, `types/company-admissions.ts` | Zod-parsed applicant request summaries/details from `/api/company-admissions/mine` and `/:id`; owner/signal-fenced. Pending join/transfer/register responses reuse the summary schema without installing target membership. |
| `sso-storage.ts` | sessionStorage `maraclaw-sso-state` / `provider` / `intent` |
| `auth-highlights.ts` | AuthShell copy for forgot/reset/verify/sso. Login/register inline their own. |
| `workspace-api.ts` | Leftover mega: agents, sessions, files, skills, tools, tasks, focus, triggers, channels (`CHANNEL_FIELDS`/`CHANNELS`), relationships, permissions, onboarding + `ONBOARDING_SKIP_KEY`. Re-exports schedule operations and `ScheduleOut` from `schedules-api.ts`; notifications live in `notifications-api.ts`. `mintFileDownloadTicket` uses Bearer-authenticated POST; `fileTicketUrl` builds a relative, same-origin redemption URL (does **not** use `apiUrl`). |
| `notifications-api.ts` | Zod-parsed notification lists and unread counts; mark-one/read-all requests under `/api/notifications`. Defines `unreadNotificationCount`. |
| `schedules-api.ts` | Zod-parsed schedule CRUD, manual runs, run receipts/cancellation and history, including cursor-paginated outcome filters. |
| `schedule-editor.ts` | `ScheduleDraft`, default draft values and changed-field patches via `scheduleChanges`. |
| `schedule-time.ts` | IANA timezone validation and next-run formatting in the schedule's timezone. |
| `openclaw-updates-api.ts` | Zod-parsed update notice details, per-agent update status and upgrade requests. |
| `openclaw-update-policy.ts` | `openclawUpdateRoute` (`updates/:noticeId`), notice/settings paths and OpenClaw update work/outlet gates. |
| `trigger-executions-api.ts` | Owner/signal-fenced, Zod-parsed cursor pages from `/api/agents/:agentId/triggers/:triggerId/executions`. |
| `trigger-executions-query.ts` | Trigger execution visibility checks and session-scoped infinite-query options. |
| `control-api.ts` | Take Control, `/credentials`, `/gogcli`, `/api/pages` (published `/p/{id}`) |
| `plaza-api.ts` | `/api/plaza/*` |
| `okr-api.ts` | `/api/okr/*` |
| `directory-api.ts` | People directory: `/api/org/users`. Provider organization directory/sync is retired. Not org departments. |
| `org-departments-api.ts` | Own-tenant org department summaries via `/api/departments/options`; org-admin name-only `PATCH /api/departments/:id`. Zod-parsed, separate from the people directory and agent grants. |
| `agent-departments-api.ts` | `GET /api/agents/:id/permissions/departments` for the permissions tab. |
| `chat/ws-client.ts` | `connectAgentChat` calls `issueChatTicket` in `auth-api.ts` for a fresh bearer-authenticated ticket per attempt, then `WS /ws/chat/{agentId}?ticket=&lang=&session_id=`. Reconnect backoff. Stops on 4001/4003. No JWT in the socket URL. |
| `attachment-preview-api.ts`, `attachment-cache.ts`, `attachment-request.ts`, `office-preview.ts` | Chat-only metadata/bytes preview; metadata reauthorizes every opening before cached display. A single owner-object/agent/path/version LRU holds at most 64 MiB, 20 MiB per entry; retirement/denial/version change evicts. Query deduplicates requests but stores no binary results. View-local object URLs are revoked; PDF/Office workers receive copies. Lazy local DOCX/XLSX/PPTX viewers disable hyperlinks, external fonts and optional engines; archive admission is 32 MiB/entry, 64 MiB total, 2000 entries, with bounded worker/load time and owned-viewer destruction. Legacy Office stays download-only. |
| `file-download.ts` | Files-tab preview state/reducer; ignores stale request events and permits one re-mint after an image error. |
| `relative-time.ts` | `relativeTime` formats timestamps with `Intl.RelativeTimeFormat`. |
| `utils.ts` | `cn()` |
| `motion.ts` | Landing tokens only (`easeOut`, `fadeUp`, …). `springSnappy` unused. `/app` does not import this. |

## WHERE TO LOOK

| Task | File |
|------|------|
| Base / WS URL | `api.ts` |
| All HTTP | `http.ts` |
| Persist member token | `auth-storage.ts` |
| Auth + tenant HTTP | `auth-api.ts` + `types/auth.ts` |
| SSO OAuth state | `sso-storage.ts` |
| AuthShell side copy | `auth-highlights.ts` |
| Agents / files / channels / onboarding | `workspace-api.ts` |
| Take Control, vault, published pages | `control-api.ts` |
| Plaza / OKR / directory | `plaza-api.ts` / `okr-api.ts` / `directory-api.ts` |
| Agent department picker | `agent-departments-api.ts` |
| Chat socket | `chat/ws-client.ts` |
| Landing motion presets | `motion.ts` |

## CONVENTIONS

- Engine I/O only through this dir. `apiRequest` / `apiFormRequest` — never page-level `fetch()`.
- New tenant/domain → new `*-api.ts`. Do **not** grow `workspace-api.ts`. Control/vault/pages already split even though agent-scoped.
- Public auth endpoints pass `token: null`.
- Chat WS is not React Query. Single-use tickets only in the query string; bearer JWT stays in HTTP headers.
- Files-tab previews and all native downloads mint a fresh 60-second, single-use ticket per action/mount. `fileTicketUrl` stays relative and same-origin; do not route through `apiUrl`. Session JWTs stay in headers, never URLs. Files-tab image errors may re-mint once; ignore stale results after selection changes/unmount. Chat attachment previews are the authenticated no-store metadata/bytes exception above, not ticket redemptions; never persist binary data, tickets or JWT URLs.
- `motion.ts` is landing-only.

## ANTI-PATTERNS

- Adding a new domain into `workspace-api.ts` (Plaza/OKR/control/directory/agent-departments already split).
- Using `directory-api.ts` for org department grants, or `agent-departments-api.ts` for the people directory.
- `fetch()` in pages; wrapping chat WS in Query.
- Using `apiUrl` for `fileTicketUrl`, caching/reusing ticket URLs, or fetching download bodies through `apiRequest` (native browser navigation follows S3 redirects).
- Importing `motion.ts` from `/app`.
- A second fetch wrapper beside `http.ts`.
