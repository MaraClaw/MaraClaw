# lib/ - engine clients

**Generated:** 2026-11-01 · Parent: `web-e/AGENTS.md`

## OVERVIEW

Engine HTTP/WS/storage layer. Pages never `fetch()`. New domain → new `*-api.ts`. Chat transport and transcript logic: [chat/AGENTS.md](./chat/AGENTS.md).

## STRUCTURE

| File | Owns |
|------|------|
| `api.ts` | `getApiBaseUrl` / `apiUrl` / `wsUrl`. Empty `VITE_API_BASE_URL` = same-origin. Rejects hostname `0.0.0.0`. |
| `http.ts` | Only fetch wrapper: `apiRequest`, `apiFormRequest`, `apiBinaryRequest`, `ApiError`, `formatApiDetail`, `userFacingRequestError`. Bearer unless `token` overridden. Owner fences cover headers/body awaits; current-owner 401 expires that session. JSON 204 → `undefined`. Binary previews stream with a 20 MiB cap, no-store and no redirects. |
| `auth-storage.ts` | localStorage `maraclaw-enduser-token` |
| `engine-contracts.generated.ts` | Generated from `../engine/contracts/api-contract.json` by `npm run contracts:generate` (`contracts:check` in CI). Never hand-edit; adapters in `types/*` and `*-api.ts` import from it. |
| `auth-api.ts` | `/api/member/auth/*` for member login/register/SSO/current user; shared `/api/auth/*` for password/tickets/tenant selection and `/api/tenants/*` for join/switch/transfer. `joinDefaultOrg` and `transferOrg` live here; `lookupOrgByEmail` / `joinSuggestedOrg` are unused by pages. Public calls pass `token: null`. |
| `invitations-api.ts` | `previewInvitation` (`POST /api/tenants/invitations/preview`, destination tenant only) and `joinInvitation` (`POST /api/tenants/join`); owner-fenced, the preview also signal-fenced. |
| `types/auth.ts` | Generated member DTO adapters: `UserOut`, `TokenResponse`, `RegisterInitResponse`, `MultiTenantResponse`, `JoinOrgResponse` (immediate token, pending or approved admission), `isTokenResponse`. Audience eligibility is enforced by the server. |
| `company-admissions-api.ts`, `types/company-admissions.ts` | Zod-parsed applicant request summaries/details from `/api/company-admissions/mine` (plus `/mine/page` for cursor pages) and `/:id`, with withdrawal; owner/signal-fenced. Pending join/transfer/register responses reuse the summary schema without installing target membership. |
| `admission-access.ts` | `hasPendingAdmission` walks pending admission pages (100 each) for a pending, non-transfer request from the current tenant; `isAdmissionAccessPath` lists the routes a restricted session may open. |
| `company-transfers-api.ts`, `types/company-transfers.ts`, `format-money.ts` | Transfer quote (`/api/company-admissions/transfer-quote`), own transfer list/detail/withdraw under `/transfers`, and the paid/free choice, quote and payment-review schemas. `formatMoney` renders minor units. |
| `sso-storage.ts` | sessionStorage `maraclaw-sso-state` / `provider` / `intent`; stores the state `authorizeProvider` returns (server-issued, intent `sign_in` or `bind`), with owner-fenced consumption |
| `auth-highlights.ts` | AuthShell copy for forgot/reset/verify/sso. Login/register inline their own. |
| `workspace-api.ts` | Facade plus leftovers. Defines session create/rename/delete, files, skills, tools, relationships, approvals, metrics and revisions. Re-exports `agents-api.ts` (agents, permissions), `agent-work-api.ts` (tasks, focus, triggers), `onboarding-api.ts` (incl. `ONBOARDING_SKIP_KEY`), `channels-api.ts` (`CHANNEL_FIELDS`/`CHANNELS`) and `schedules-api.ts`; notifications live in `notifications-api.ts`. `mintFileDownloadTicket` uses Bearer-authenticated POST; `fileTicketUrl` builds a relative, same-origin redemption URL (does **not** use `apiUrl`). |
| `notifications-api.ts` | Zod-parsed notification pages and unread counts; single, all and selected read/unread/archive/restore requests under `/api/notifications`. Defines `unreadNotificationCount`. |
| `chat-sessions-api.ts`, `chat-sessions-query.ts` | Generated-schema parsing of `GET /api/agents/:id/sessions/page` (`scope`, `limit` 50, opaque `cursor`; the shared session leads the first page only) and the single-session lookup `GET /api/agents/:id/sessions/:sessionId`. Keys: chat `['sessions', agentId, scope]` (infinite, `mine` or `all`), Take Control `['sessions', agentId, 'control', 'mine']` (first page only), URL-opened lookup `['sessions', agentId, 'item', sessionId]`; `distinctSessions` drops ids a later page repeats. |
| `chat-history-api.ts`, `chat-history-query.ts` | Zod-parsed `GET /api/agents/:id/sessions/:id/messages/page` (50 rows, cursor; `has_more` must match `next_cursor`). Latest polls send the held revision and message boundary for unchanged, bounded delta, or reset replies. Deltas retain the earlier-page cursor; resets replace the retained range. Earlier pages are cached per revision/cursor and prepended; a cursor that fails to advance throws `ChatHistoryCursorError`. Merge rules: `chat/history.ts`. `workspace-api.listSessionMessages` (`limit=200`, no cursor) is unused. |
| `activity-api.ts` | Manager activity pages from `/api/agents/:id/activity/page` (limit 1-200, cursor, `action_type`, `no-store`). |
| `agent-lifecycle-api.ts`, `agent-lifecycle-policy.ts`, `agent-setup-api.ts`, `agent-deletion-api.ts` | Lifecycle status and optional-MCP retry, setup status/retry, expiry and deletion-request policy, and `/api/agent-deletion-requests`. |
| `agent-skills-api.ts` | `listInstalledAgentSkills`: skills under the agent's `skills/` folder (`SKILL.md`/`skill.md` entry point or flat `*.md`), owner- and signal-fenced. |
| `upload-policy-api.ts`, `whatsapp-api.ts`, `whatsapp-personal-session.ts` | `GET /api/chat/upload-policy` limits; WhatsApp channel and personal-QR calls; a view-local personal session whose secrets never enter a query cache. |
| `schedules-api.ts` | Zod-parsed schedule CRUD, manual runs, run receipts/cancellation and history, including cursor-paginated outcome filters. |
| `schedule-editor.ts` | `ScheduleDraft`, default draft values and changed-field patches via `scheduleChanges`. |
| `schedule-time.ts` | IANA timezone validation and next-run formatting in the schedule's timezone. |
| `openclaw-updates-api.ts` | Zod-parsed update notice details, per-agent update status and upgrade requests. |
| `openclaw-update-policy.ts` | `openclawUpdateRoute` (`updates/:noticeId`), notice/settings paths and OpenClaw update work/outlet gates. |
| `trigger-executions-api.ts` | Owner/signal-fenced, Zod-parsed cursor pages from `/api/agents/:agentId/triggers/:triggerId/executions`. |
| `trigger-executions-query.ts` | Trigger execution visibility checks and session-scoped infinite-query options. |
| `control-api.ts` | Take Control, `/credentials`, `/gogcli`, `/api/pages` (published `/p/{id}`) |
| `clawbook-api.ts` | `/api/clawbook/*`; Zod-parsed keyset pages for posts and comments (`/posts/page`, `/posts/:id/comments/page`), opaque cursors. |
| `clawbook-query.ts` | Clawbook post/comment infinite-query options, id dedupe, and the publish mutation that truncates the feed to page one before refetching it. |
| `okr-api.ts` | `/api/okr/*` |
| `directory-api.ts` | People directory: `/api/org/users`. Provider organization directory/sync is retired. Not org departments. |
| `org-departments-api.ts` | Own-tenant org department summaries via `/api/departments/options`; org-admin name-only `PATCH /api/departments/:id`. Zod-parsed, separate from the people directory and agent grants. |
| `agent-departments-api.ts` | `GET /api/agents/:id/permissions/departments` for the permissions tab. |
| `chat/ws-client.ts` | `connectAgentChat` calls `issueChatTicket` in `auth-api.ts` for a fresh bearer-authenticated ticket per attempt, then `WS /ws/chat/{agentId}?ticket=&lang=&session_id=`. Reconnect backoff. Stops on 4001/4003. No JWT in the socket URL. |
| `chat/live-stream.ts` | Live text reducer keyed by gateway `turn_id`, with monotonic `seq`, replacement snapshots and bounded completed-turn tracking. Ignore chunks from another active or completed turn. A foreign `done` still adds history but preserves the active stream; matching final content is authoritative. Assistant `done.message_id` is a different identifier. |
| `attachment-preview-api.ts`, `attachment-cache.ts`, `attachment-request.ts`, `office-preview.ts` | Chat-only metadata/bytes preview; metadata reauthorizes every opening before cached display. A single owner-object/agent/path/version LRU holds at most 64 MiB, 20 MiB per entry; retirement/denial/version change evicts. Query deduplicates requests but stores no binary results. View-local object URLs are revoked; PDF/Office workers receive copies. Lazy local DOCX/XLSX/PPTX viewers disable hyperlinks, external fonts and optional engines; archive admission is 32 MiB/entry, 64 MiB total, 2000 entries, with bounded worker/load time and owned-viewer destruction. Legacy Office stays download-only. |
| `file-download.ts` | Files-tab preview state/reducer; ignores stale request events and permits one re-mint after an image error. |
| `relative-time.ts` | `relativeTime` formats timestamps with `Intl.RelativeTimeFormat`. |
| `utils.ts` | `cn()` |
| `motion.ts` | Landing tokens only (`easeOut`, `fadeUp`, …). `springSnappy` unused. `/app` does not import this. |

## WHERE TO LOOK

- Join, transfer, admission or tenant switch: `invitations-api.ts`, `company-transfers-api.ts`, `company-admissions-api.ts`, `auth-api.ts`, then `types/*`.
- Agent files, sessions, skills, tools or channels: the `workspace-api.ts` facade, or the concrete `agents-api.ts` / `agent-work-api.ts` / `channels-api.ts` it re-exports.
- Take Control, vault, published pages: `control-api.ts`. Clawbook, OKR, directory: their own `*-api.ts`.
- Chat socket: `chat/ws-client.ts`. Chat history, skill mentions, transcripts: `chat-history-*.ts` and the `chat/` guide.

## CONVENTIONS

- Engine I/O only through this dir. `apiRequest` / `apiFormRequest` — never page-level `fetch()`.
- New tenant/domain → new `*-api.ts`. Do **not** grow `workspace-api.ts`. Control/vault/pages already split even though agent-scoped.
- Public auth endpoints pass `token: null`.
- Chat WS is not React Query. Single-use tickets only in the query string; bearer JWT stays in HTTP headers.
- Files-tab previews and all native downloads mint a fresh 60-second, single-use ticket per action/mount. `fileTicketUrl` stays relative and same-origin; do not route through `apiUrl`. Session JWTs stay in headers, never URLs. Files-tab image errors may re-mint once; ignore stale results after selection changes/unmount. Chat attachment previews are the authenticated no-store metadata/bytes exception above, not ticket redemptions; never persist binary data, tickets or JWT URLs.
- `motion.ts` is landing-only.

## ANTI-PATTERNS

- Adding a new domain into `workspace-api.ts` (Clawbook/OKR/control/directory/agent-departments already split).
- Using `directory-api.ts` for org department grants, or `agent-departments-api.ts` for the people directory.
- `fetch()` in pages; wrapping chat WS in Query.
- Using `apiUrl` for `fileTicketUrl`, caching/reusing ticket URLs, or fetching download bodies through `apiRequest` (native browser navigation follows S3 redirects).
- Importing `motion.ts` from `/app`.
- A second fetch wrapper beside `http.ts`.
