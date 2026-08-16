# lib/ - engine clients

**Generated:** 2026-08-16 · Parent: `web-l/AGENTS.md`

## OVERVIEW

Engine HTTP/WS/storage layer. Pages never `fetch()`. New domain → new `*-api.ts`.

## STRUCTURE

| File | Owns |
|------|------|
| `api.ts` | `getApiBaseUrl` / `apiUrl` / `wsUrl`. Empty `VITE_API_BASE_URL` = same-origin. Rejects hostname `0.0.0.0`. |
| `http.ts` | Only fetch wrapper: `apiRequest`, `apiFormRequest`, `ApiError`, `formatApiDetail`, `userFacingRequestError`. Bearer unless `token` overridden. 401+token → `clearStoredToken`. 204 → `undefined`. |
| `auth-storage.ts` | localStorage `maraclaw-enduser-token` |
| `auth-api.ts` | `/api/auth/*` and `/api/tenants/*` (login/register/SSO/password/join/switch). Public calls pass `token: null`. |
| `types/auth.ts` | `UserOut`, `TokenResponse`, `MultiTenantResponse`, `isPlatformAdminUser`, `isTokenResponse` |
| `sso-storage.ts` | sessionStorage `maraclaw-sso-state` / `provider` / `intent` |
| `auth-highlights.ts` | AuthShell copy for forgot/reset/verify/sso. Login/register inline their own. |
| `workspace-api.ts` | Leftover mega: agents, sessions, files, skills, tools, tasks, schedules, focus, triggers, channels (`CHANNEL_FIELDS`/`CHANNELS`), relationships, permissions, notifications, onboarding + `ONBOARDING_SKIP_KEY`. `fileDownloadUrl` is same-origin only (does **not** use `apiUrl`). |
| `control-api.ts` | Take Control, `/credentials`, `/gogcli`, `/api/pages` (published `/p/{id}`) |
| `plaza-api.ts` | `/api/plaza/*` |
| `okr-api.ts` | `/api/okr/*` |
| `directory-api.ts` | `/api/org/users`, `/api/enterprise/org/{members,departments}` |
| `chat/ws-client.ts` | `connectAgentChat` → `WS /ws/chat/{agentId}?token=&lang=&session_id=`. Reconnect backoff. Stops on 4001/4003. Token in query string (engine contract). |
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
| Chat socket | `chat/ws-client.ts` |
| Landing motion presets | `motion.ts` |

## CONVENTIONS

- Engine I/O only through this dir. `apiRequest` / `apiFormRequest` — never page-level `fetch()`.
- New tenant/domain → new `*-api.ts`. Do **not** grow `workspace-api.ts`. Control/vault/pages already split even though agent-scoped.
- Public auth endpoints pass `token: null`.
- Chat WS is not React Query. Token in query string (engine contract).
- `fileDownloadUrl` stays same-origin; do not route through `apiUrl`.
- `motion.ts` is landing-only.

## ANTI-PATTERNS

- Adding a new domain into `workspace-api.ts` (Plaza/OKR/control/directory already split).
- `fetch()` in pages; wrapping chat WS in Query.
- Using `apiUrl` for `fileDownloadUrl`.
- Importing `motion.ts` from `/app`.
- A second fetch wrapper beside `http.ts`.
