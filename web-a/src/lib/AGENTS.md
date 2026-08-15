# web-a/src/lib

Admin HTTP + auth storage. Parent: `web-a/AGENTS.md`.

## OVERVIEW

Thin fetch clients. No React Query here except callers. Engine owns behavior.

## WHERE TO LOOK

| File | Role |
|------|------|
| `api.ts` | `getApiBaseUrl` / `apiUrl`. Empty `VITE_API_BASE_URL` = same-origin (Vite `/api` proxy). Hostname `0.0.0.0` → same-origin (listen address is not a browser dest). |
| `http.ts` | `apiRequest`, `ApiError`, `formatApiDetail`. Bearer from storage unless `token: null`. 401 + stored token → `clearStoredToken()` (does not reset AuthContext). |
| `auth-storage.ts` | `maraclaw-admin-token` only. |
| `auth-api.ts` | login, `/me`, forgot/reset/change password. |
| `companies-api.ts` | list/create/toggle companies + email-domain CRUD. |
| `linkup-keys-api.ts` | platform-admin list/add/remove of Linkup API keys. |
| `types/auth.ts` | `UserOut` / `must_change_password` helpers (OR token + user + identity). |

## CONVENTIONS

- Public `VITE_*` only. Never ship `PLATFORM_ADMIN_*`.
- Platform admin may pass `tenant_id`; org admin is own-tenant (enforce in the client + UI).
- Treat `403 { must_change_password: true }` as force-change, not logout.
- New engine surfaces: add a domain `*-api.ts` here; do not fetch from components.

## ANTI-PATTERNS

- Hardcoded absolute API hosts.
- Sharing `maraclaw-enduser-token` or `maraclaw-theme`.
- Inventing REST handlers in Vite.
