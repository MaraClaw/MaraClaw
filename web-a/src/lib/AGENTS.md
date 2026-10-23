# web-a/src/lib

Thin fetch clients. Parent: `../../AGENTS.md`. Callers own React Query.

## OVERVIEW

`apiRequest` wrappers + DTOs. Engine owns behavior. Request bodies **snake_case**.

## WHERE TO LOOK

| File | Role |
|------|------|
| `api.ts` | `getApiBaseUrl` / `apiUrl`. Empty `VITE_API_BASE_URL` = same-origin. Hostname `0.0.0.0` → `''`. |
| `http.ts` | `apiRequest`, `ApiError`, `formatApiDetail`. Bearer unless `token: null`. 401 + stored token → `clearStoredToken()` only (AuthContext stays). 204 → `undefined`. |
| `auth-storage.ts` | `maraclaw-admin-token` only. |
| `auth-api.ts` | login, `/me`, forgot/reset/change. Public calls pass `token: null`. Accepts `signal`; `AbortSignal` appears in 14 lib `.ts` files, including other API clients and session helpers. |
| `admin-session.ts` | Shared `adminSession` policy backed by `auth-storage.ts` token storage. |
| `session-policy.ts` | Session generation, cancellation, token reconciliation and stale-operation fencing via `runOwned`. |
| `auth-session.ts` | Framework-independent login, bootstrap/refresh and logout with session-owned cancellation and stale-result fencing. |
| `login-schema.ts` | Zod email normalization and login password validation. |
| `companies-api.ts` | list/create/toggle + email-domain CRUD. `listCompanies(q)` prefix FTS. `getTenant` **pads** a `CompanyStats` (SSO/counts empty). |
| `users-api.ts` | list/detail/activate members + OA + PA. `asAdminUser` / `setOrgAdminActive` flatten into `AdminUser` + `source`. |
| `departments-api.ts` | `GET/POST /api/departments`, `PUT /api/users/{id}/department`. Always sends `tenant_id`; tenant-local directory only. Provider organization sync is retired. |
| `linkup-keys-api.ts` | PA list/add/remove Linkup keys. Response is fingerprint, never plaintext. |
| `llm-models-api.ts` | Org/platform admin LLM pool. Keys only on write; list returns `api_key_masked`. `withKnownProviders` always includes Grok. Grok subscription retains device-code start/status. |
| `chatgpt-subscription-api.ts` | Validated browser sign-in start (`attempt_id`, `authorization_url`, `redirect_uri`, `expires_in`), complete and read-only status. Optional start `tenant_id`; complete sends `{attempt_id, callback_url}` only in its POST body; status sends only `attempt_id`. No tokens. Errors discard potentially sensitive echoed details. |
| `search-analytics-api.ts` | PA summary / timeseries / orgs / trending / export. |
| `admin-audit-api.ts` | Validated recent audit logs: PA `/api/admin/audit-logs`, OA `/api/users/admin-audit-logs`. |
| `admissions-api.ts` | Review pagination, detail and decisions via `/api/company-admissions`; Zod DTOs. |
| `broadcast-api.ts` | Validated broadcast payload/result for `POST /api/notifications/broadcast`. |
| `notification-links.ts` | Allowlist internal admin notification destinations; unsafe links return `null`. |
| `notifications-api.ts` | Current-user notification pages, unread count, mark-read and mark-all-read. |
| `openclaw-update-storage.ts` | Admin-scoped `sessionStorage` for validated OpenClaw update requests. |
| `openclaw-updates-api.ts` | Validated update notices + request IDs for `POST /api/admin/openclaw-updates`. |
| `tools-api.ts` | Catalog list/create/update/delete via `/api/tools`; list accepts tenant scope + `signal`. |
| `types/auth.ts` | `UserOut` + guards (`isAdminUser`, `isPlatformAdminUser`, `userMustChangePassword`). |

## CONVENTIONS

- New engine surface → new `*-api.ts` here. Do not `fetch` from pages.
- Query string: `encodeURIComponent` or `URLSearchParams`. Omit `method` = GET; body without method = POST.
- Adapters stay here when engine shape ≠ UI: `getTenant` pads `CompanyStats`; `asAdminUser` / `setOrgAdminActive` flatten PA/OA.

## ANTI-PATTERNS

- React Query in this folder (callers own it).
- Expecting Linkup create/list to return the raw key.
