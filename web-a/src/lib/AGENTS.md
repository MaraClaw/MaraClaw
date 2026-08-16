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
| `auth-api.ts` | login, `/me`, forgot/reset/change. Public calls pass `token: null`. `signal` only exists here. |
| `companies-api.ts` | list/create/toggle + email-domain CRUD. `listCompanies(q)` prefix FTS. `getTenant` **pads** a `CompanyStats` (SSO/counts empty). |
| `users-api.ts` | list/detail/activate members + OA + PA. `asAdminUser` / `setOrgAdminActive` flatten into `AdminUser` + `source`. |
| `linkup-keys-api.ts` | PA list/add/remove Linkup keys. Response is fingerprint, never plaintext. |
| `llm-models-api.ts` | Org/platform admin LLM pool. Keys only on write; list returns `api_key_masked`. `withKnownProviders` always includes Grok. |
| `search-analytics-api.ts` | PA summary / timeseries / orgs / trending / export. |
| `types/auth.ts` | `UserOut` + guards (`isAdminUser`, `isPlatformAdminUser`, `userMustChangePassword`). |

## CONVENTIONS

- New engine surface → new `*-api.ts` here. Do not `fetch` from pages.
- Query string: `encodeURIComponent` or `URLSearchParams`. Omit `method` = GET; body without method = POST.
- Adapters stay here when engine shape ≠ UI: `getTenant` pads `CompanyStats`; `asAdminUser` / `setOrgAdminActive` flatten PA/OA.

## ANTI-PATTERNS

- React Query in this folder (callers own it).
- Expecting Linkup create/list to return the raw key.
