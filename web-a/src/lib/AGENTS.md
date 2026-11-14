# web-a/src/lib

Thin fetch clients. Parent: `../../AGENTS.md`. Callers own React Query.

## OVERVIEW

`apiRequest` wrappers + DTOs. Engine owns behavior. Request bodies **snake_case**.

## WHERE TO LOOK

| File | Role |
|------|------|
| `api.ts` | `getApiBaseUrl` / `apiUrl`. Empty `VITE_API_BASE_URL` = same-origin. Hostname `0.0.0.0` → `''`. |
| `http.ts` | `apiRequest` (= `createApiRequest(adminSession, fetch)`), `ApiError`, `formatApiDetail`. Optional Zod-like `schema` parses the body; optional `idempotencyKey` → `Idempotency-Key` header. 204 → `undefined`. Also `apiFormRequest(path, form, options?)` (multipart: the body is the `FormData`, no JSON `Content-Type`, so the browser sets the boundary; default method POST) and `apiBinaryRequest(path, options?)` (`Uint8Array`, `no-store`, no redirects, streamed with a 20 MiB `BinaryLimitError` cap). All three share one `execute`, so owner fences and the 401 policy are identical. Keep every call in `src/lib/*-api.ts` with a literal path: `engine/tests/test_client_route_resolution.py` scans this folder. See 401 policy below. |
| `capacity-api.ts`, `capacity-pricing.ts`, `capacity-forms.ts`, `capacity-format.ts`, `capacity-order-state.ts` | Extra accounts and agents. `capacity-api.ts`: Zod-parsed `/api/capacity/*` (overview, orders list/create/get/cancel, evidence upload and download) and `/api/admin/capacity-orders*` + `/api/admin/capacity-payment-settings`; `capacityConflict` reads the `409 {code, order_id?}` body. `capacity-pricing.ts`: `quoteCapacity` is the engine formula (`capacity_pricing.py`) in `BigInt`, `quoteFromOverview` the Users-page preview. `capacity-forms.ts`: draft schemas (counts, decision note, settings) whose messages are translation keys, plus `evidenceFileProblem` (advisory; the engine checks the leading bytes). `capacity-format.ts`: VND/USD/file size and calendar-day formatting (`YYYY-MM-DD` is formatted in UTC so no time zone shifts it). `capacity-order-state.ts`: status predicates, `isRenewalLapsed` (unpaid renewals stop counting the day after `grace_until`), `vietnamToday`. `capacity-errors.ts`: `CapacityGuardError` (the local 403 a mutation throws before any request, carrying a translation key) and `capacityEvidenceErrorKey` / `capacityCancelErrorKey` / `capacityGuardKey`, which map the evidence and cancel refusals (409 `order_not_awaiting_payment` / `order_not_cancellable`, 413 whether from the engine or nginx, 415, 503, 403, 404) to plain translation keys; `null` keeps the engine's message. |
| `auth-storage.ts` | `maraclaw-admin-token` only, plus `listenForTokenChanges` (cross-tab `storage` events). |
| `auth-api.ts` | login, `/me`, forgot/reset/change. Public calls pass `token: null`. Reads accept `signal`; new reads should too. |
| `admin-session.ts` | The shared `adminSession` policy over `auth-storage.ts`. |
| `session-policy.ts` | `SessionOwner` `{generation, token, signal}`; `capture`/`reconcile`, `isCurrent`/`assertCurrent`, `subscribe`, `replace`, `logout`, `expire`; `runOwned` fences both outcomes of a transport. |
| `auth-session.ts` | Framework-independent login, bootstrap/refresh and logout; expires non-admin sessions; stale results dropped. |
| `login-schema.ts` | Zod email normalization and login password validation. |
| `engine-contracts.generated.ts` | Generated Zod schemas/enums from `../engine/contracts/api-contract.json`. Never hand-edit; wrap with `.strip()`/`.extend()` in `*-api.ts`. Regenerate with `npm run contracts:generate`, verify with `contracts:check` (both in `web-a/package.json`). |
| `companies-api.ts` | list/create/toggle + email-domain CRUD; `fetchCompanyAgentSections` / `updateCompanyAgentSections` (`/api/admin/companies/{id}/agent-sections`, `{sections, hidden_sections}`). `listCompanies(q)` prefix FTS. `getTenant` **pads** a `CompanyStats` (SSO/counts empty). |
| `users-api.ts` | Company roster: `listUsersPage` (`/api/users/page`, `tenant_id`, `search`, `limit=50`, opaque `cursor`, Zod page `{items,next_cursor}`) and `exportCompanyUsers` (`/api/users/export`, `{csv}` only). `listUsers` array form and `listPlatformAdmins` (unpaged, filtered client-side) remain. Detail, activate for members, OA and PA; `asAdminUser` / `setOrgAdminActive` flatten into `AdminUser` + `source`. `isEndUserRole` = `member` or `agent_admin`. |
| `departments-api.ts` | Tenant-local directory under `/api/departments` (list, create, rename, delete) and `PUT /api/users/{id}/departments` (`setUserDepartments` replaces a member's list; single-department `setUserDepartment` is legacy). Delete preview: `.../deletion-preview`. Merge: `.../merge-preview` returns a `confirmation_token`, `.../merge` POSTs `{destination_id, confirmation_token}`. Always sends `tenant_id`. Provider organization sync is retired. |
| `invitations-api.ts`, `invitation-form-schema.ts` | `/api/enterprise/invite-users` and `invitation-codes` (page size 20, TTL cap 336h); form schema converts semicolon emails and days to hours. SMTP failures map to Settings guidance. |
| `tenant-email-api.ts`, `tenant-email-access.ts` | Org SMTP under `/api/enterprise/tenant-email`; scope only for an active genesis `org_admin`/`platform_admin` with a tenant. Errors drop echoed detail (may contain credentials). |
| `transfer-policy-api.ts` | `/api/admin/transfer-policy`: fee in minor units, currency, instructions. Settings shows it to platform admins only. |
| `agent-deletion-api.ts`, `agent-transfers-api.ts`, `agent-lifecycle-errors.ts` | Deletion queue/detail/approve under `/api/agent-deletion-requests` (`canApproveAgentDeletion` needs loaded, pending, `can_approve`, `has_viewed`, not `has_approved`). Transfers POST `/api/agent-ownership-transfers` (1-100 unique agents, source ≠ recipient); `canTransferMemberAgents`, `isAgentTransferTarget`, all-cursor recipient fetch. Structured error codes map to fixed messages. |
| `transfer-reviews-api.ts`, `transfer-review-access.ts` | Payment queue (server-paged `/transfers/review/page`, `view` `pending|recent`, limit 50, opaque `cursor`, page `{items,next_cursor}`), detail, decision under `/api/company-admissions/transfers`. `canReviewTransferPayments`: active PA only, using the canonical platform flag or role, without a tenant lookup. `isPendingTransferPayment` = paid + `payment_review`. |
| `tavily-keys-api.ts` | PA list/add/remove Tavily keys under `/api/admin/tavily-keys`. Secrets use the `tvly-` prefix. Response is fingerprint, never plaintext. |
| `llm-models-api.ts` | Org/platform admin LLM pool. Keys only on write; list returns `api_key_masked`. `withKnownProviders` always includes Grok. Grok subscription retains device-code start/status. |
| `chatgpt-subscription-api.ts` | Validated browser sign-in start (`attempt_id`, `authorization_url`, `redirect_uri`, `expires_in`), complete and read-only status. Optional start `tenant_id`; complete sends `{attempt_id, callback_url}` only in its POST body; status sends only `attempt_id`. No tokens. Errors discard potentially sensitive echoed details. |
| `search-analytics-api.ts` | PA summary / timeseries / orgs / trending / export. |
| `resource-pressure-api.ts`, `resource-pressure-format.ts`, `csv-download.ts` | PA `/api/admin/resource-pressure/{summary,episodes,export.csv}`. Strict filter schema (`since` inclusive, `until` exclusive, offset instants; `tenantId`, `agentId`, `kind`, `openOnly` are episode-side); episodes `limit=25` + opaque `cursor` (≤512). `export.csv` is a Bearer GET (no ticket): `exportResourcePressureCsv` returns the text through `apiRequest` (a CSV never parses as JSON), `downloadCsvFile` saves it as a Blob. Format: bytes→GiB/MiB, millicpus→vCPU, ratios→%, durations, row view-models. |
| `admin-audit-api.ts`, `admin-audit-window.ts` | Validated audit logs: PA `/api/admin/audit-logs`, OA `/api/users/admin-audit-logs`, each with a `/page` cursor variant (`start_at`/`end_at` window, `target_type`). OA scope omits tenant and actor filters. |
| `admissions-api.ts` | Review pagination, detail and decisions via `/api/company-admissions`; Zod DTOs. |
| `broadcast-api.ts` | Validated broadcast payload/result for `POST /api/notifications/broadcast`, plus email status and cancel. `sendBroadcast(draft, idempotencyKey)` requires a key; `reviewBroadcast` reuses an unconfirmed attempt's key only for an identical draft, `attemptAfterFailure` drops it on 409, `broadcastFailureMessage` maps 409 to the key-reused copy. |
| `notification-links.ts` | Allowlist internal admin notification destinations (deletion and `/capacity-payments/{id}` / `/capacity-orders/{id}` links need an exact UUID); unsafe links return `null`. |
| `notifications-api.ts` | Current-user notification pages (`view` is `active` or `archived`), unread count, single/all/selected read, unread, archive and restore. |
| `openclaw-update-storage.ts` | Admin-scoped `sessionStorage` for validated OpenClaw update requests. |
| `openclaw-updates-api.ts` | Validated update notices + request IDs for `POST /api/admin/openclaw-updates`. |
| `tools-api.ts` | Catalog list/create/update/delete via `/api/tools`; list accepts tenant scope + `signal`. |
| `types/auth.ts` | `UserOut` + guards (`isAdminUser`, `isPlatformAdminUser`, `userMustChangePassword`) and the capacity predicates (`canBuyCapacity`, `canViewCapacityOrders`, `canReviewCapacityPayments`, `canManageCapacityPaymentSettings`). |

## SESSION AND 401 POLICY

- `apiRequest` captures the current `SessionOwner`; an explicit `token` that is not the stored one fails as stale. `token: null` sends no `Authorization` and has no owner, so a public 401 never expires a session.
- Cancellation is `AbortSignal.any([owner.signal, options.signal])`. Ownership is re-asserted after `fetch` and after the body is read, so a retired session's success, failure or 401 becomes `StaleSessionError`.
- A 401 on a still-current owner calls `policy.expire(owner)`: clears the stored token, bumps `generation`, aborts the old signal with `StaleSessionError`, and notifies subscribers with reason `expired` (`AuthProvider` resets to anonymous and retires the Query client). The 401 `ApiError` is still thrown to that caller.
- `expire` is a no-op (returns `false`) for a tokenless or already-stale owner; `replace`, `logout` and storage reconciliation also bump generation. 403 never expires.
- Server writes already accepted are not undone by cancellation.

## CONVENTIONS

- New engine surface → new `*-api.ts` here. Do not `fetch` from pages.
- Parse engine bodies through generated schemas (`.strip()` then `.extend()` for stricter local rules) rather than hand-typing DTOs.
- Query string: `encodeURIComponent` or `URLSearchParams`. Omit `method` = GET; body without method = POST.
- Adapters stay here when engine shape ≠ UI: `getTenant` pads `CompanyStats`; `asAdminUser` / `setOrgAdminActive` flatten PA/OA.

## ANTI-PATTERNS

- React Query in this folder (callers own it).
- Expecting Tavily create/list to return the raw key.
