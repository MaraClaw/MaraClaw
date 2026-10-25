# Admin APIs - Platform admin & Tenant admin

**Audience:** Agents and humans building `web-a` (admin console), reviewing RBAC, or wiring clients against the engine.  
**Source of truth:** FastAPI routers under `engine/app/api/`, mounted with `API_PREFIX=/api` (`engine/app/config.py`, `engine/app/main.py`).  
**Generated from codebase review:** 2026-08-13 (branch work). Re-check routers if routes drift.

In product language:

| Product term | Code role | Scope |
|--------------|-----------|--------|
| **Platform admin** | `platform_admin` | Cross-tenant; platform metrics/settings; no company-admission override |
| **Tenant admin** | `org_admin` | Company admin for the caller’s `tenant_id` |

Also accepted in some gates: `identity.is_platform_admin` elevates like `platform_admin` when that role is allowed.

---

## Auth model

Password login accepts `{ login_identifier, password, tenant_id? }`. The wire key
`login_identifier` is retained, but its value is trimmed and validated as an email
address at the HTTP boundary. Email lookup is case-insensitive; phone numbers and
account handles are not password-login identifiers. Registration requires only
`email` and `password`, with optional `display_name` defaulting to the email local
part. Account responses, profile edits, and availability checks have no username
field.

Fresh schemas omit `identities.username` and its index. An existing nullable
column is left untouched and is neither read nor written by the engine; no live
database migration is required by this source change. Existing session-cache
payloads ignore the removed field without a cache wipe. Provider IDs and external
service handles remain independent. Presentation-only aliases such as chat
`username` and task `creator_username` contain display names, email addresses, or
a neutral label, never an account handle. Messaging tools retain the `username`
argument as an email/display-name alias, require an unambiguous match, and keep
the caller's exact tenant scope (including no tenant).

| Mechanism | Location | Behavior |
|-----------|----------|----------|
| Bearer JWT | `get_current_user` | `Authorization: Bearer <token>` |
| Shared admin | `get_current_admin` (`app/core/security.py`) | Requires `platform_admin` **or** `org_admin` (or identity platform flag) |
| Exact roles | `require_role("…")` | Exact match; identity platform flag counts if `platform_admin` is listed |
| Hierarchy | `ROLE_HIERARCHY` | `member` < `agent_admin` < `org_admin` < `platform_admin` |

### Member auth audience

The engine selects the member audience from `/api/member/auth`, never from `Origin`,
`Referer`, or a JWT audience claim. These routes share the existing credential,
verification, registration, SSO, password-change and session-generation behavior:

| Method | Member path | Response |
|--------|-------------|----------|
| `POST` | `/api/member/auth/login` | `MemberTokenResponse` or `MultiTenantResponse` |
| `POST` | `/api/member/auth/register/init` | `MemberRegisterInitResponse` (201) |
| `POST` | `/api/member/auth/verify-email` | `MemberTokenResponse` |
| `POST` | `/api/member/auth/{provider}/callback` | `MemberTokenResponse` or `MultiTenantResponse` |
| `GET`, `PATCH` | `/api/member/auth/me` | `MemberAuthUser` |
| `POST` | `/api/member/auth/{provider}/bind`, `/unbind` | `MemberAuthUser` |

`org_admin`, `agent_admin`, and `member` are eligible. A platform-admin membership
or `identity.is_platform_admin` rejects member sign-in with **403**:
`{"detail":{"code":"member_audience_rejected","message":"Platform operators sign in on the admin console."}}`.
Correct credentials are checked before this password-login rejection; an incorrect
password remains **401**. Eligibility is checked before session issuance or OAuth
pending-token caching, including redemption of a previously cached OAuth step.
Known operator identities are rejected before registration admission or identity-link
side effects. Eligible multi-company selection, tenantless onboarding and forced
password changes retain their existing behavior. Member `GET /me` remains available
while a password change is required; member profile writes retain the force-change gate.

Member user and identity DTOs project domain records independently and never expose
`is_platform_admin`, including nested token fields. Shared `/api/auth/*` and `/api/sso/*`
retain their existing operator access and JSON contracts. The broad public model remains
named `AuthUser`, not `AdminAuthUser`, because it is also used by shared auth, SSO and
user APIs rather than exclusively by the admin console.

Password changes (`PUT /api/auth/me/password`), tenant switching
(`POST /api/auth/switch-tenant`) and QR scan status (`GET /api/sso/session/{sid}/status`)
remain shared. Before installing a token from these flows, member clients must refresh
through `GET /api/member/auth/me` and accept only a successful member projection.
Password recovery, verification resend, OAuth authorization and provider discovery
also remain shared; they do not issue a member user DTO. This is a route-level sign-in
boundary, not a new JWT claim or a replacement for resource authorization.

### Genesis admin bootstrap

| Account | How created | First-login rule |
|---------|-------------|------------------|
| **Platform admin** | Startup uses genesis PA credentials already in the database. If those are missing, env vars `PLATFORM_ADMIN_EMAIL` + `PLATFORM_ADMIN_PASSWORD` seed or repair them. Fail-closed if the env vars are also missing. | Must change password after first successful login (`identity.must_change_password`) |
| **Genesis org admin** | Platform admin only: `POST /api/tenants` or `POST /api/admin/companies` with `admin_email` + `admin_password` | Must change password after first successful login |
| **Additional platform admin** | Genesis platform admin only: `POST /api/admin/platform-admins` | Must change password after first successful login |
| **Additional org admin** | Genesis org admin only: `POST /api/users/org-admins` (or `PATCH /api/users/{id}/role`) | New accounts must change password after first login |

Open registration never elevates to `platform_admin`. Bootstrap never elevates an existing email unless `PLATFORM_ADMIN_PASSWORD` verifies against that identity, then forces password change. Genesis platform admin membership is the **MaraClaw** system org. Genesis is stored on `users.is_genesis` and cannot be moved by role change, assign-user, or join.

While `must_change_password` is true, login still issues a token, but `get_current_user`, chat-ticket issuance, file download, tenant join, and admin gates return **403** until `PUT /api/auth/me/password` (or a password reset) clears the flag. WebSocket chat closes with **4003** if that requirement arises after ticket issuance. New password must differ from the current password. `GET /api/auth/me` and password change use `get_authenticated_user` and remain available. Tenant creation is platform-admin only (`POST /api/tenants` or `POST /api/admin/companies`). Platform settings expose invitation-code and SSO-domain redirect toggles only.

Chat clients request `POST /api/auth/chat-ticket` with Bearer authentication and JSON `{agent_id, session_id?}`. The **200** response `{ticket, session_id}` has `Cache-Control: no-store`; an omitted session resolves or creates the authorized primary web session. Connect to `/ws/chat/{agent_id}?ticket=...&session_id=...&lang=...` using that exact session. JWT query authentication is not supported. Tickets are single-use, expire unredeemed after 60 seconds, and cannot extend the issuing JWT's expiry. Invalid, replayed, or mismatched tickets close with **4002** (obtain another ticket); expired authentication or an inactive principal closes with **4001**, and denied authorization closes with **4003**. Redis failure returns **503** during issuance or closes the socket with **1011**. Principal and session authorization are checked freshly at redemption and on every inbound message; the original JWT deadline also bounds idle sockets and in-flight work.

Fresh-only installations use `POST /api/auth/register/init` for password registration and `POST /api/auth/{provider}/callback` for OAuth registration/login. The obsolete `/api/auth/register`, `/api/auth/register/sso`, and `/api/auth/email-hint` routes are unavailable (404). Registration/token responses expose `needs_org_confirm` and optional `suggested_org`; `needs_company_setup` has been removed. The inert `allow_self_create_company` setting is no longer exposed or writable.

Password-reset and verify links prefer the requesting frontend `Origin` (or Referer origin) when it is listed in `CORS_ORIGINS` — so `web-a` (`:5174`) and `web-e` (`:5173`) each receive their own links. Unknown origins are ignored; fallback is `PUBLIC_BASE_URL` (the end-user site, `web-e`).

`PUBLIC_BASE_URL` is deployment-owned and loaded through `Settings` from the environment or `.env`. Request hosts are never a fallback. Without a configured base, webhook-URL endpoints, SSO redirects (including stored tenant domains), and invitation emails return **503** with a configuration error. SSO enablement still updates its flag but does not assign a domain. Published pages retain relative `/p/<id>` links with a configuration note, and webhook-trigger creation returns an error without creating a trigger. Without either a configured base or an allowlisted frontend origin, verification mail is skipped and logged while registration succeeds; forgot-password keeps its generic response. `PUT /api/enterprise/system-settings/platform` rejects any `public_base_url` key with **422**; configure `PUBLIC_BASE_URL` in the deployment instead.

Base path for most routes: **`/api`**.

Self-prefixed exceptions (no double-prefix): `okr` → `/api/okr`, plus a few public/websocket routers (see `app/api/AGENTS.md`).

---

## A. Platform-admin only

### A1. Platform company console - `/api/admin/*`

**Router:** `app/api/admin.py`  
**Auth:** `require_role("platform_admin")` on every route.

| Method | Path | Request | Response / notes |
|--------|------|---------|------------------|
| `GET` | `/api/admin/companies` | Query: `q?` (max 200) | `CompanyStats[]` including `user_count`, `active_user_count`, `inactive_user_count`, `agent_count`. Empty `q` lists newest-first. Non-empty `q` is prefix full-text search on `name` + `slug` (`mara` matches `MaraClaw`), ranked, limited to 50. |
| `POST` | `/api/admin/companies` | `{ name: string(1–200), admin_email: email, admin_password: string(6–128), admin_display_name?: string }` | **201** `{ company: CompanyStats, org_admin_email, must_change_password: true }` - creates tenant + genesis org admin (initial password must be changed on first login). Claims the admin email host as the company default email domain (`techadmin@marathon.vn` → `marathon.vn`). **409** if the email or that domain is already claimed. |
| `PUT` | `/api/admin/companies/{company_id}/toggle` | - | `{ ok, is_active }` - **400** for MaraClaw/OpenClaw (`is_system` or `is_default_end_user_org`). Disable deactivates org members (not `platform_admin`), stops agents, and turns off triggers/schedules. Enable restores members; agents/automations stay stopped. |
| `GET` | `/api/admin/metrics/timeseries` | Query: `start_date`, `end_date` (datetime) | Daily series: companies, users, tokens, cache, sessions, DAU/WAU/MAU, cache hit rate |
| `GET` | `/api/admin/metrics/leaderboards` | - | `{ top_companies[], top_agents[] }` (top 20 by tokens + cache stats) |
| `GET` | `/api/admin/metrics/enhanced` | - | avg tokens/session 30d, 7d retention, channel distribution, tool category top10, churn warnings |
| `POST` | `/api/admin/platform-admins` | `{ admin_email, admin_password, admin_display_name? }` | **201** `{ user_id, admin_email, must_change_password: true }` - **genesis platform admin only**. Duplicate email → **409**. |
| `GET` | `/api/admin/platform-admins` | - | Platform admin list with `is_genesis` / `is_active` |
| `PATCH` | `/api/admin/platform-admins/{user_id}/active` | `{ is_active }` | Genesis PA only. Cannot target self or genesis. Deactivating the last active PA is blocked. |
| `GET` | `/api/admin/audit-logs` | Query: `tenant_id?`, `actor_id?`, `action?`, `limit=100` | Admin action trail: actor, action, time, `changes` before/after |
| `GET` | `/api/admin/audit-logs/page` | Query: `tenant_id?`, `actor_id?`, `action?`, `limit=100`, `cursor?` | `{ items, next_cursor }`; deterministic older-event seek pagination. Page size clamped to 1-500. |
| `GET` | `/api/admin/platform-settings` | - | `{ invitation_code_enabled, sso_custom_domain_redirect_enabled }` |
| `PUT` | `/api/admin/platform-settings` | Same fields optional | Updated `PlatformSettingsOut` |
| `GET` | `/api/admin/linkup-keys` | - | Stored Linkup key ring (id, label, fingerprint, status, position). Never returns ciphertext. **Router:** `app/api/admin_linkup.py`. |
| `POST` | `/api/admin/linkup-keys` | `{ label, api_key }` | **201** add a key. Missing/blank `label` or `api_key` → **422**. Duplicate secret → **409**. Appends to the rotation cycle. |
| `DELETE` | `/api/admin/linkup-keys/{key_id}` | - | Remove a key and retarget the cursor. **404** if missing. |
| `GET` | `/api/admin/search-analytics/summary` | Query: `start?`, `end?`, `tenant_id?` | Platform-only. Omit `tenant_id` for **entire system**; pass it to scope one org. Adds `scope: system\|org`. Counts, kind mix, unique orgs/agents, unattributed. Default last 7 days, max 90. No raw queries. **Router:** `app/api/admin_search_analytics.py`. |
| `GET` | `/api/admin/search-analytics/timeseries` | Query: `start?`, `end?`, `tenant_id?` | Daily volume + errors. Same system vs org rule. |
| `GET` | `/api/admin/search-analytics/orgs` | Query: `start?`, `end?`, `limit=20` | Top tenants by search volume (system-wide upsell list). |
| `GET` | `/api/admin/search-analytics/trending` | Query: `start?`, `end?`, `tenant_id?`, `scope?`, `limit=50` | Omit `tenant_id` for system-wide trends (ranked by companies then hits). Pass `tenant_id` for one org. Never returns raw text. |
| `GET` | `/api/admin/search-analytics/export-status` | - | Outbox lag, last export, capture/export flags. |

### A2. Tenants - platform-only pieces

**Router:** `app/api/tenants.py`

| Method | Path | Auth | Request | Response / notes |
|--------|------|------|---------|------------------|
| `GET` | `/api/tenants/` | platform_admin | - | `TenantOut[]` all tenants |
| `POST` | `/api/tenants/` | platform_admin | `{ name, admin_email, admin_password, admin_display_name? }` | **201** `{ tenant: TenantOut, org_admin_email, must_change_password: true }` - creates tenant + genesis org admin. Duplicate admin email → **409**. Replaces `POST /api/tenants/self-create`. |
| `PUT` | `/api/tenants/{tenant_id}/assign-user/{user_id}` | platform_admin | Query: `role` ∈ `agent_admin` \| `member` (default `member`) | `{ status, user_id, tenant_id, role }`. Same-company role changes only; company changes return **409** and require admission approval. Cannot assign `org_admin`, reassign genesis, or demote the last active admin. Clears `identity.is_platform_admin` when demoting a PA. |
| `POST` | `/api/tenants/{tenant_id}/genesis-org-admin` | genesis platform_admin | `{ admin_email, admin_password, admin_display_name? }` | **201** attach a genesis OA when the tenant has none. **409** if one already exists or the email is taken. |

**SSO note:** On `PUT /api/tenants/{tenant_id}`, platform admins **cannot** set `sso_enabled` / `sso_domain` (stripped server-side). SSO is managed by the company’s own org admin via Enterprise settings / identity providers.

---

## B. Tenant admin + platform admin (shared)

Unless noted: both `org_admin` and `platform_admin` work. Platform may cross tenants (often via `tenant_id` query/body); org admin is limited to own `tenant_id`.

### B1. Tenants / company settings - `/api/tenants/*`

**Router:** `app/api/tenants.py`

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/tenants/{tenant_id}` | platform / org | - | Org: own tenant only. **Res:** `TenantOut` |
| `PUT` | `/api/tenants/{tenant_id}` | platform / org | `TenantUpdate`: optional `name`, `im_provider`, `timezone`, `country_region`, `is_active`, `sso_enabled`, `sso_domain`, `a2a_async_enabled` | Org: own tenant. Platform: SSO fields stripped. **`is_active` is platform_admin only** (403 otherwise). Cannot disable system or default end-user orgs (MaraClaw / OpenClaw). Setting `is_active=false` uses the same member/agent/automation cascade as company toggle. |
| `GET` | `/api/tenants/{tenant_id}/email-domains` | platform / org | - | Claimed email domains for the company |
| `POST` | `/api/tenants/{tenant_id}/email-domains` | platform / org | `{ domain, is_default? }` | **201**. **409** if another company already claims the domain |
| `PATCH` | `/api/tenants/{tenant_id}/email-domains/{domain_id}` | platform / org | `{ is_default: true }` | Make this the default invite domain |
| `DELETE` | `/api/tenants/{tenant_id}/email-domains/{domain_id}` | platform / org | - | **204**. Promotes another default when needed |
| `POST` | `/api/tenants/{tenant_id}/logo` | platform / org | Multipart image PNG/JPEG/WebP, ≤1 MB, 1:1 square | `TenantOut` |
| `DELETE` | `/api/tenants/{tenant_id}/logo` | platform / org | - | `TenantOut` |
| `GET` | `/api/tenants/{tenant_id}/deletion-preview` | platform_admin | - | Read-only `{ users_count, agents_count, schedules_count }`. Org admins (including genesis) receive **403**. |
| `DELETE` | `/api/tenants/{tenant_id}` | platform_admin | - | Cascade delete. `{ status: "deleted", fallback_tenant_id }`. Org admins (including genesis) receive **403**. |

#### `TenantOut` shape

```text
id, name, slug, im_provider, timezone, country_region, is_active,
sso_enabled, sso_domain, a2a_async_enabled, default_model_id, logo_url, created_at,
is_system, is_default_end_user_org, can_disable
```

#### Related non-admin tenant routes (context only)

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/api/tenants/join` | Invitation join for **unassigned** users only: a valid recipient-bound code admits immediately, **200** `{ status: "approved", request: RequestSummary }`. Any existing member (OpenClaw or company) gets **409** and must use `/transfer`. Invalid, expired, spent or wrong-recipient codes are **400**. |
| `POST` | `/api/tenants/invitations/preview` | Body `{ invitation_code }`. Authenticated eligible unassigned/default-org member only. **200** `{ tenant: { id, name } }`; every other outcome is the same **400** `Invitation is invalid or no longer available`. Read-only. |
| `POST` | `/api/tenants/transfer` | Body `{ password, invitation_code?, tenant_id?, transfer_choice?, free_destructive_consent? }`. Password required. A company target requires `invitation_code` (**403** otherwise; no verified-domain path to a company); a valid code admits at once, **200** `{ status: "approved", request }`, after the default-source, free-consent and paid-fee rules (paid stays **202** `payment_review` until payment is approved, then admits without destination approval). OpenClaw targets remain immediate. Protected admins blocked. |
| `POST` | `/api/tenants/join-suggested` | Verified email-domain company admission request, **202** pending for active OpenClaw or unassigned users. Company members must use `/transfer` (**409**). Protected admins blocked. |
| `POST` | `/api/tenants/join-default` | Decline the match and join OpenClaw. Active user required. Genesis / platform admin blocked. |
| `GET` | `/api/tenants/lookup-by-email` | Public. `{ match, fallback }` for registration UI. Does not attach. |
| `GET` | `/api/tenants/registration-config` | Public. Always `{ tenant_creation: "platform_admin_only" }`. |
| `DELETE` | `/api/tenants/{tenant_id}` | Platform admin only. Cascade delete, then tombstone identities with no remaining membership. **400** if the tenant is a system org or the default end-user org. |
| `GET` | `/api/tenants/resolve-by-domain` | Public SSO domain resolve |
| `GET` | `/api/tenants/me` | Any member; company + default model |
| `GET` | `/api/tenants/me/token-usage` | Any member; token/cache aggregates |
| `GET` | `/api/tenants/{tenant_id}/logo` | Public asset by UUID |

#### Company admission approval

Company join, join-suggested and transfer return **202**
`{ status: "pending", request: RequestSummary }`. A request does not create target
membership: the applicant keeps their source organization until approval.
Repeated requests for the same pending target return the same request without a
second notification; a different pending target returns **409**.

Invitation registration returns **201** with `admission_request: RequestSummary | null`;
its user and token still represent OpenClaw (or the existing source membership).
Fresh SSO and OAuth tenant selection also stage admission rather than writing a
company membership. New authenticated SSO users are active even when SMTP email
verification is configured; existing disabled accounts remain disabled.
Already-admitted SSO users and genesis provisioning remain
unchanged. IM lazy registration cannot create company members; applicants must
complete web admission first. Admin assign-user only changes roles within the
existing company and cannot move users between companies.

| Method | Path | Access / response |
|--------|------|-------------------|
| `GET` | `/api/company-admissions/mine?limit=50&offset=0` | Applicant's `RequestSummary[]` |
| `GET` | `/api/company-admissions/review?status=pending&limit=50&offset=0` | Active persisted target genesis org admin only; `RequestDetail[]` |
| `GET` | `/api/company-admissions/{id}` | Applicant or target genesis org admin; `RequestDetail` |
| `POST` | `/api/company-admissions/{id}/decision` | Target genesis org admin only; `{ decision: "approved" | "rejected", reason: string | null }`; `RequestDetail` |

`RequestSummary` contains `id`, `status` (`pending`, `approved`, `rejected`), nullable
`source_tenant_id`, `target_tenant: { id, name }`, `method` (`invitation`,
`verified_domain`, `sso`), `created_at`, nullable `decided_at`, and nullable
`decision_reason`. `RequestDetail` adds `applicant: { id, email }`, nullable
`source_tenant: { id, name }`, and nullable `decided_by_user_id`.

Additional org admins and platform admins cannot review or decide. Decisions
re-read active persisted genesis authorization after tenant-first locks. Approval
atomically moves membership through the membership service, validates and consumes
the invitation when applicable, resolves the request and notifies the applicant.
Source drift blocks approval (**409**) but still permits rejection. Repeating the
same decision is idempotent; a conflicting final decision returns **409**.

Notifications participate in the same transaction. Both types belong to the
`approval` category: `company_admission_pending` links the genesis admin to
`/admission-approvals/{id}`; `company_admission_resolved` links the applicant to
`/app/admissions/{id}`. Fresh schema baseline includes the request table and a
partial unique index enforcing one pending request per applicant; this change
does not include an existing-database migration.

### B2. Users - `/api/users/*`

**Router:** `app/api/users.py`

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/users/` | platform / org | Query: `tenant_id?` (platform only) | `AdminUserSummary[]` with quotas + `agents_count` |
| `GET` | `/api/users/{user_id}` | platform / org | - | `UserDetailOut`: user fields plus `agents[]` (`id`, `name`, `status`, `is_expired`, `role_description`, `last_active_at`). Org admin: own company only. |
| `PATCH` | `/api/users/{user_id}/active` | platform / org | `{ is_active }` | Activate or deactivate an **end user** (`member` or `agent_admin`). Org admin: own company only. Cannot target self or admins (use org-admin / platform-admin active routes). Deactivate also stops that user's agents and turns off their triggers/schedules. `AdminUserSummary` |
| `PATCH` | `/api/users/{user_id}/quota` | platform / org | `UserQuotaUpdate`: `quota_message_limit?`, `quota_message_period?` (`permanent`\|`daily`\|`weekly`\|`monthly`), `quota_max_agents?`, `quota_agent_ttl_hours?` | Same-tenant only. `AdminUserSummary` |
| `POST` | `/api/users/org-admins` | genesis org_admin | `{ admin_email, admin_password, admin_display_name? }` | **201** `{ user_id, tenant_id, admin_email, must_change_password: true }`. Own tenant only. |
| `GET` | `/api/users/org-admins` | org_admin | - | Org admins in the caller's company (`is_genesis`, `is_active`) |
| `PATCH` | `/api/users/org-admins/{user_id}/active` | genesis org_admin | `{ is_active }` | Other org admins in own company only. Cannot target self or genesis. |
| `GET` | `/api/users/admin-audit-logs` | org_admin | Query: `action?`, `limit=100` | Company-scoped admin action trail |
| `GET` | `/api/users/admin-audit-logs/page` | org_admin | Query: `action?`, `limit=100`, `cursor?` | `{ items, next_cursor }`; tenant comes only from the authenticated principal. Page size clamped to 1-500. |
| `PATCH` | `/api/users/{user_id}/role` | platform / org | `{ role }` | **Genesis** org admin may set `org_admin` \| `member`. **Genesis** platform admin may set `platform_admin` \| `member`. Other admins may only set `member`. Genesis rows cannot change role. Blocks demoting the last *active* admin. |

#### Audit page navigation

Both `/page` endpoints order events by `created_at DESC, id DESC`, preserving timestamp
microseconds. Pass `next_cursor` unchanged to fetch older events; `null` means there is no
next page. Cursors are versioned, limited to 1024 characters, and bound to the effective
role, tenant and filters. Malformed or mismatched cursors return **400** after authorization;
the existing force-password-change gate still applies. Changing filters requires a fresh
root request without a cursor. These pages are not a snapshot: refresh from the root to
include new inserts or late commits. The existing array endpoints remain unchanged.

#### `AdminUserSummary` (users router)

```text
id, email, display_name, role, is_active, is_genesis, tenant_id,
quota_message_limit, quota_message_period, quota_messages_used,
quota_max_agents, quota_agent_ttl_hours, agents_count, created_at, source
```

#### Org department memberships

**Router:** `app/api/departments.py`. Org departments are organization-owned and manually managed; provider organization department/people sync is retired. Org admins operate in their own tenant; platform admins must select `?tenant_id=UUID` for administration.

| Method | Path | Request | Response / notes |
|--------|------|---------|------------------|
| `GET` | `/api/departments` | Query: `tenant_id?` | `{ departments, assignments }`; assignments retain one `{ user_id, tenant_id, department_id }` row per membership, ordered by user then department UUID. |
| `POST` | `/api/departments` | `{ name }` | **201** department. Names are trimmed; duplicate case-insensitive name → **409**. |
| `PATCH` | `/api/departments/{department_id}` | `{ name }` | Rename without changing membership rows. |
| `PUT` | `/api/users/{user_id}/departments` | `{ department_ids: UUID[] }` | Full-set replacement for `member` / `agent_admin`. Returns `{ user_id, tenant_id, department_ids }` with unique UUIDs in ascending order. `[]` clears all; missing/null/non-array input → **422**. Any missing or foreign department rejects the complete set with **422**, without changing memberships. |
| `PUT` | `/api/users/{user_id}/department` | `{ department_id: UUID \| null }` | Existing singular contract: replace with one membership or clear all. Returns `{ user_id, tenant_id, department_id }`. |

Members may belong to multiple org departments. Department agent grants are the union of memberships and confer only `use`, never `manage`; direct agent permissions are unchanged. Replacement freshly authorizes the actor and subject under tenant-first locks, then invalidates every agent affected by either the old or new set in the same transaction. Tenant transfer and promotion out of end-user roles clear all org department memberships; user deletion cascades membership removal.

### B3. Org user profile - `/api/org/*`

**Router:** `app/api/organization.py`

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/org/users` | any auth; admins may pass `tenant_id` | Query: `tenant_id?` | Active users, display-name ordered |
| `PATCH` | `/api/org/users/{user_id}` | admin (`get_current_admin`) | `UserUpdate`: `email?`, `display_name?`, `avatar_url?`, `title?`, `primary_mobile?` | Uniqueness checks; updates shared org-member contact |

### B4. Enterprise suite - `/api/enterprise/*`

**Router:** `app/api/enterprise.py`  
Default admin gate: `get_current_admin` unless noted.

#### LLM pool

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/enterprise/llm-providers` | admin | - | Provider registry manifest. Members **403**. |
| `POST` | `/api/enterprise/llm-test` | admin | `provider`, `model`, `api_key?`, `base_url?`, `model_id?` | `{ success, latency_ms, reply? \| error? }`. `model_id` must be in the caller’s company. ChatGPT subscription rows call Codex `/responses`, not `/chat/completions`. |
| `GET` | `/api/enterprise/llm-models` | auth | Query: `tenant_id?` | Members: enabled catalog only; **no** key / base URL. Admin: full pool + masked key. `is_default` = company primary, `is_secondary` = cheap/routine lane, `is_fallback` = failover. Non-platform cannot other tenants. Untenanted members get `[]`. |
| `POST` | `/api/enterprise/llm-models` | admin | `LLMModelCreate` + Query `tenant_id?` | **201** `LLMModelOut`. Org admin locked to own tenant. Platform admin must pick a company. |
| `POST` | `/api/enterprise/llm-models/grok-subscription/start` | admin | Query `tenant_id?` | Device-code start. `{ session_id, verification_url, user_code, expires_in, interval }`. **Never** returns tokens, cookies, or `device_code`. |
| `GET` | `/api/enterprise/llm-models/grok-subscription/status` | admin | Query `session_id` | `{ status, session_id, verification_url?, user_code?, model_id?, detail? }`. On `authorized`, engine persists an encrypted company Grok row. Org admin cannot poll another tenant. |
| `POST` | `/api/enterprise/llm-models/chatgpt-subscription/start` | admin | Query `tenant_id?` | Browser OAuth PKCE: `{ attempt_id, authorization_url, redirect_uri, expires_in }`. Org admin uses own company; platform admin selects one. Private verifier and tokens never leave the engine. |
| `POST` | `/api/enterprise/llm-models/chatgpt-subscription/complete` | admin | JSON `{ attempt_id, callback_url }`; extra fields forbidden | Validates the pasted callback locally, atomically claims the attempt, exchanges the code, and commits the encrypted company ChatGPT row and pool activation. `{ status, attempt_id, model_id?, detail?, expires_in? }`. Status: `pending`, `authorized`, `expired`, `denied`, or `error`. |
| `GET` | `/api/enterprise/llm-models/chatgpt-subscription/status` | admin | Query `attempt_id` | Same status shape as complete. Read-only recovery: **no** provider calls, token exchange, or model writes. Unknown/expired attempts return `expired`. |
| `POST` | `/api/enterprise/llm-models/{model_id}/refresh-subscription` | admin | - | Refresh stored SuperGrok / X Premium or ChatGPT tokens. `{ ok, model_id, expires_at? }`. No token fields. |
| `POST` | `/api/enterprise/llm-models/{model_id}/set-default` | admin | - | Sets company **primary**; may migrate agents. Clears secondary/fallback if it was this model. **204**. Org admin cannot touch another company. |
| `POST` | `/api/enterprise/llm-models/{model_id}/set-secondary` | admin | - | Sets company **secondary** (manageable tasks). Must differ from primary and fallback. Applies to agents with the previous secondary or none. **204**. |
| `POST` | `/api/enterprise/llm-models/{model_id}/set-fallback` | admin | - | Sets company **fallback**; must differ from primary and secondary. Applies to agents with the previous fallback or none. **204**. |
| `PUT` | `/api/enterprise/llm-models/{model_id}` | admin | `LLMModelUpdate` (partial) | `LLMModelOut`. Same tenant isolation. |
| `DELETE` | `/api/enterprise/llm-models/{model_id}` | admin | Query: `force?` | **409** if agents use model unless forced. **204**. Same tenant isolation. |

**ChatGPT browser handoff:** Open `authorization_url` in the administrator's browser. After sign-in, copy the full address-bar URL beginning `http://localhost:1455/auth/callback` into the completion form, even if the local page cannot connect. The engine neither listens on that local port nor fetches the pasted URL. There is no ChatGPT device-code fallback. Each Redis-backed attempt lasts **600 seconds**, is bound to the initiating administrator's membership, identity and target company, and stores the private verifier encrypted. A valid completion claims it once (`GETDEL`); malformed URLs or mismatched state return **400** without consuming it, a different owner returns **404**, and replay returns **409**. Terminal results are owner-bound and retained for **300 seconds** for a lost-response retry through status. Redis unavailability returns **503**, never a local-memory fallback. Active administrator, identity, password-change and company checks run freshly; the model snapshot must remain unchanged after the token exchange. No database checkout is held during provider I/O, and `authorized` is published only after the persistence/activation transaction commits. Access and refresh tokens remain encrypted; refresh and Codex Responses inference continue to use the stored subscription.

**`LLMModelCreate`:** `provider`, `model`, `api_key`, `base_url?`, `label`, `temperature?` (0–2), `max_tokens_per_day?`, `enabled`, `supports_vision`, `max_output_tokens?`, `request_timeout?`.

#### Enterprise info, approvals, audit, stats

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/enterprise/info` | auth | - | `EnterpriseInfoOut[]` |
| `PUT` | `/api/enterprise/info/{info_type}` | admin | `{ content: object, visible_roles: string[] }` | Upsert + sync to agents |
| `GET` | `/api/enterprise/approvals` | auth | Query: `tenant_id?`, `status_filter?` | Platform broader; others creator-scoped |
| `POST` | `/api/enterprise/approvals/{approval_id}/resolve` | auth | `{ action: "approve" \| "reject" }` | `ApprovalRequestOut` |
| `GET` | `/api/enterprise/audit-logs` | admin | Query: `agent_id?`, `tenant_id?`, `limit=50` | `AuditLogOut[]` |
| `GET` | `/api/enterprise/stats` | admin | Query: `tenant_id?` | `{ total_agents, running_agents, total_users, pending_approvals }` |

#### Tenant quotas (caller’s tenant)

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/enterprise/tenant-quotas` | auth + tenant | - | Default limits: messages, agents, TTL, LLM calls/day, heartbeat floor, triggers, poll/webhook ceilings |
| `PATCH` | `/api/enterprise/tenant-quotas` | admin | Same fields optional | May adjust existing agent heartbeats. `{ message, heartbeat_agents_adjusted }` |

#### Organization SMTP (own organization only)

Source: `app/api/tenant_email.py`, `app/schemas/tenant_email.py`,
`app/services/tenant_email_admin.py` and `app/services/email_config.py`.

All four routes require an active administrator with `is_genesis=true`, role
`org_admin` or `platform_admin`, and their own non-null `tenant_id`. Additional
admins cannot view, configure, delete or test SMTP. Optional query `tenant_id`
defaults to the caller's organization; a foreign organization is forbidden
(**403**), including for platform admins. The password-change gate still applies.
For SMTP, MaraClaw and OpenClaw are ordinary isolated organizations with no
cross-organization privileges. The `web-a` control belongs only in Settings,
visible to eligible admins, with no organization picker.

| Method | Path | Request | Response / notes |
|--------|------|---------|------------------|
| `GET` | `/api/enterprise/tenant-email` | Query: `tenant_id?` | **200** `TenantEmailOut`: saved fields below plus `has_password: boolean`, `effective_source: "tenant" \| "none"`. Never returns `password` or ciphertext. |
| `PUT` | `/api/enterprise/tenant-email` | Query: `tenant_id?`; JSON `TenantEmailUpdate` below | **200** `TenantEmailOut`. Replaces non-password fields; omitted fields take their defaults. An omitted password preserves the saved secret. |
| `DELETE` | `/api/enterprise/tenant-email` | Query: `tenant_id?` | **204**, no body. Removes the saved configuration. |
| `POST` | `/api/enterprise/tenant-email/test` | Query: `tenant_id?`; no request body | **200** `{ message: "Test email sent to your administrator email address" }`. Tests the saved configuration, even when disabled, and sends only to the caller's own administrator email. No arbitrary recipient or unsaved SMTP settings. |

`TenantEmailUpdate` accepts these fields; extra fields are forbidden:

| Field | Type / constraints | Default |
|-------|--------------------|---------|
| `enabled` | boolean; enabling requires `from_address` and `smtp_host` | `false` |
| `from_address` | email address, or empty while disabled; max 320 characters | `""` |
| `from_name` | string, max 200 characters; empty stays empty | `""` |
| `smtp_host` | hostname or IP address, not a URL; max 253 characters | `""` |
| `smtp_port` | integer, 1..65535 | `465` |
| `smtp_username` | string, max 320 characters | `""` |
| `smtp_ssl` | boolean | `true` |
| `smtp_timeout_seconds` | integer, 1..30 | `15` |
| `password` | optional write-only string, max 4096 characters; encrypted at rest | Omitted, null or empty preserves saved password |

`has_password` reports whether a secret is stored. `effective_source="tenant"`
means the enabled saved configuration has a host and sender; otherwise it is
`"none"`. This reports configuration selection, not SMTP connectivity.
Disabling or deleting the configuration stops tenant email delivery. Invitations
and broadcast email require that tenant's own enabled SMTP; another
organization is never a fallback. A test requires a saved
host and sender and an administrator email address; missing prerequisites or an
SMTP test failure return **400**. Testing does not enable delivery.

#### System email & settings

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/enterprise/email-templates` | admin | - | templates + variables + defaults |
| `PUT` | `/api/enterprise/email-templates` | admin | `{ templates: object }` | Persist templates |
| `GET` | `/api/enterprise/system-settings/{key}` | auth | - | `{ key, value, updated_at? }`; retired SMTP key is unavailable. |
| `PUT` | `/api/enterprise/system-settings/{key}` | admin; `platform` platform_admin only | `{ value: object }` | `platform` may regenerate all SSO domains; retired SMTP key is unavailable. |
| `GET` | `/api/enterprise/system-settings/notification_bar/public` | public | - | Notification bar config |

There is only one SMTP configuration path: encrypted organization settings.
Identity verification/password-reset mail and platform incident/admission mail
resolve the system organization with slug `maraclaw` and use only its enabled,
complete saved SMTP. Missing or disabled MaraClaw SMTP means no delivery, never
an OpenClaw or other organization fallback. Explicit organization delivery never
consults MaraClaw as a fallback. The platform genesis administrator must configure
their own MaraClaw SMTP in Settings before sending email broadcasts.

The retired key `system_email_platform` returns **404** for GET and PUT for every
role, before any stored value is read or written. Existing retired rows remain
untouched and are not used for delivery. `/api/enterprise/system-email/test` has
been removed; use the own-organization test route above.

#### Identity providers / SSO

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/enterprise/identity-providers` | auth | Query: `tenant_id?`, `global_only?` | `global_only` platform only. Secrets sanitized |
| `POST` | `/api/enterprise/identity-providers` | admin | `provider_type`, `name`, `is_active`, `sso_login_enabled`, `config`, `tenant_id?` | Org: own tenant. Platform may create global Google/GitHub without tenant |
| `POST` | `/api/enterprise/identity-providers/oauth2` | admin | OAuth2 fields: `app_id`, `app_secret`, URLs, `scope?` | Tenant required |
| `PATCH` | `/api/enterprise/identity-providers/{provider_id}/oauth2` | admin | Partial OAuth2 fields | Tenant ownership for non-platform |
| `PUT` | `/api/enterprise/identity-providers/{provider_id}` | admin | `name?`, `is_active?`, `sso_login_enabled?`, `config?` | Syncs tenant SSO state |
| `DELETE` | `/api/enterprise/identity-providers/{provider_id}` | admin | - | **204** |

#### Provider organization sync (retired)

Provider department and people sync is no longer available. The `/api/enterprise/org/*` directory/sync endpoints and public WeCom organization verification/callback endpoints are removed. This does not remove WeCom SSO or messaging channels.

The active people directory remains `GET /api/org/users`; org departments and memberships remain under `/api/departments` and `/api/users/{user_id}/departments`. Shared `org_members` identity/contact/profile data, transliteration fields and agent relationship references remain. Relationship responses retain `department_path` as an empty string.

Fresh `scripts/schema_baseline.sql` initialization no longer creates `org_departments` or `org_members.department_id`, `department_path`, `synced_at`. This is a fresh-only source change: rerunning bootstrap does not remove existing database objects or data, and no upgrade patches are provided.

**Existing-installation deployment gate:** Do not roll this change out onto a populated legacy schema without a separately approved transition/cleanup plan. Retained `org_departments` rows can block tenant deletion through their non-cascading tenant foreign key now that application cleanup no longer queries that table. Provider deletion can leave inert legacy department rows; the legacy table has no provider foreign key. No existing database cleanup is performed by this change.

#### Invitations (admin **and** caller must have `tenant_id`)

| Method | Path | Request | Response / notes |
|--------|------|---------|------------------|
| `POST` | `/api/enterprise/invitation-codes` | `{ email, count=1, ttl_hours=336 }` (`email` required; `ttl_hours` 1-336; count ≤ 100; `max_uses` may only be 1) | `{ created, codes[] }`. Codes are 22-character random tokens, bound to the normalized `email`, single-use, expiring at a server-computed time |
| `POST` | `/api/enterprise/invite-users` | `{ emails: EmailStr[], ttl_hours=336 }` | Requires the caller's tenant SMTP, configured and enabled; no platform fallback. One recipient-bound single-use code per address; the mail carries the link and expiry. `{ invited, message }` |
| `GET` | `/api/enterprise/invitation-codes` | Query: `page`, `page_size`, `search` | Paginated list. Items: `id, code, max_uses, used_count, is_active, created_at` plus `expires_at, invited_email, redeemed_at` (latest redemption) and derived `status` (`revoked` > `used` > `expired` > `active`) |
| `GET` | `/api/enterprise/invitation-codes/export` | - | CSV download |
| `DELETE` | `/api/enterprise/invitation-codes/{code_id}` | - | Soft-deactivate |

### B5. Google Workspace directory authorization (retired)

The Google Workspace admin-sync authorization endpoint, stored sync authorization state and sync callback are removed with provider directory sync. Google Workspace SSO/login, Google OAuth, gogcli user authorization and Google Chat channel behavior remain supported independently; their authentication contracts are unchanged.

### B6. Enterprise knowledge base - `/api/enterprise/knowledge-base/*`

**Router:** `enterprise_kb_router` in `app/api/files.py`

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/enterprise/knowledge-base/files` | auth + tenant | Query: `path?` | List dir entries |
| `POST` | `/api/enterprise/knowledge-base/upload` | **admin** | Multipart `file`, `sub_path?` | May auto-extract text from binaries |
| `GET` | `/api/enterprise/knowledge-base/content` | auth | Query: `path` | File content |
| `PUT` | `/api/enterprise/knowledge-base/content` | **admin** | Query: `path` + body content | Write file |
| `DELETE` | `/api/enterprise/knowledge-base/content` | **admin** | Query: `path` | File or tree delete |

### B7. Tools catalog - `/api/tools/*`

**Router:** `app/api/tools.py`  
Catalog mutations retain `get_current_user` (including the force-password-change gate) and `_require_catalog_manager`. Platform-admin authority follows `UserRecord.is_platform_admin` (role or loaded identity flag); org admins use `role=org_admin`. Only platform admins may target another company via `tenant_id`.

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/tools` | auth | Query: `tenant_id?` | Tenant-scoped catalog; secrets masked; required `tenant_id: string \| null` and `capabilities` per record |
| `POST` | `/api/tools` | admin | `ToolCreate`: name, display_name, description, type, category, icon, schemas, MCP fields, `is_default`, `tenant_id?` | `{ id, name }` |
| `PUT` | `/api/tools/bulk` | admin + record rights | `[{ tool_id, enabled }]` | `{ ok: true }`; preload and authorize every existing record before any write; missing IDs silently skipped |
| `PUT` | `/api/tools/{tool_id}` | admin + policy below | `ToolUpdate` (partial metadata; full config replacement) | `{ ok: true }`; mixed config + metadata requests require record rights before any write |
| `DELETE` | `/api/tools/{tool_id}` | admin + record rights | - | `type=builtin` remains 400; forbidden records return 403 before assignment deletion |

Record rights and configuration are evaluated by `app/services/tool_ownership.py`:

| Record / operation | Platform admin | Org admin with concrete own tenant | Tenantless org admin / other roles |
|--------------------|----------------|------------------------------------|------------------------------------|
| Non-builtin source, concrete own-tenant row: metadata, enabled/default, bulk, row config | Yes | Yes | No |
| Non-builtin source, shared (`tenant_id=null`) or foreign row: same mutations | Yes | No | No |
| Builtin source: metadata, enabled/default, bulk | Yes | No, even if malformed row has own-tenant tag | No |
| Builtin source: config-only replacement | Concrete authorized target required; any company | Own concrete target only | No |
| Delete (`type != builtin`) | Any record | Only non-builtin source with non-null matching tenant | No |
| Delete (`type=builtin`) | No (400) | No (400) | No |

`capabilities` has three required booleans: `can_edit_details` follows record rights; `can_delete` additionally requires `type != builtin`; `can_configure` follows record rights for non-builtin sources and requires a concrete authorized target for builtin sources. `tenant_id` is the stored record owner, not the selected target. Member catalog reads remain available with all capabilities false. Existing response keys are unchanged; agent assignment/config response shapes do not gain these catalog-only fields.

Agent-level tool assignment/config routes exist under `/api/tools/agents/...` but use agent access checks (not pure admin gates). **Only** platform/org may set `allow_network` / proxy fields on agent tool config.

`search_x` and `generate_image_grok` read `config.api_key` from this catalog, then fall back to `XAI_API_KEY`. Custom `base_url` is only accepted for `https://api.x.ai`. web-a `/tools` supports catalog inspection, MCP creation, capability-gated editing, explicit configuration replacement and deletion. Platform admins select `?company=`; org admins remain in their own tenant. Configuration PUT is a full replacement, not a merge: the UI omits config during metadata writes and requires existing secrets to be re-entered before a confirmed replacement. Builtin-source config is company-local in `tenant_settings`; non-builtin config is a catalog-row write. Metadata or mixed config + metadata on an unowned record rejects the entire request before any config/DAO write. Creation and `/mcp-server` request/response contracts are unchanged.

### B8. Skills - `/api/skills/*`

**Router:** `app/api/skills.py`

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `POST` | `/api/skills/` | admin | `name`, `description`, `category`, `icon`, `folder_name`, `files[{path,content}]` | Create skill |
| `PUT` | `/api/skills/{skill_id}` | admin | partial skill update | Platform: all. Org: own-tenant + builtins |
| `DELETE` | `/api/skills/{skill_id}` | admin | - | Same write rules |
| `GET` | `/api/skills/settings/token` | org_admin / platform_admin | - | GitHub/ClawHub status (masked) |
| `PUT` | `/api/skills/settings/token` | org_admin / platform_admin | `{ github_token?, clawhub_key? }` | Requires tenant |
| `PUT` | `/api/skills/browse/write` | admin | `{ path, content }` | Path-based skill file write |
| `DELETE` | `/api/skills/browse/delete` | admin | Query: `path` | Delete file or whole skill folder |

### B9. Notifications

**Router:** `app/api/notification.py`

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/notifications` | authenticated user | Query: `view=active\|archived` (default `active`), `category?`, `unread_only=false`, `limit=50`, `offset=0` | Own notifications only, newest first; each item includes nullable `archived_at`. Invalid view returns **422**. |
| `GET` | `/api/notifications/unread-count` | authenticated user | Query: `category?` | `{ unread_count }` counts only active unread notifications. |
| `POST` | `/api/notifications/{id}/read` | authenticated user | - | `{ ok: true }`; reading an archived notice does not restore it. |
| `POST` | `/api/notifications/read-all` | authenticated user | Query: `category?` | `{ ok: true }`; marks matching active notices read, including unloaded pages, without changing archived notices. |
| `POST` | `/api/notifications/{id}/archive` | authenticated user | - | `{ ok: true }`; owner-scoped and idempotent, preserving `is_read` and the first archive timestamp. Missing or foreign IDs return the same **404**. |
| `POST` | `/api/notifications/{id}/restore` | authenticated user | - | `{ ok: true }`; owner-scoped and idempotent, clearing `archived_at` without changing `is_read`. Missing or foreign IDs return the same **404**. |
| `POST` | `/api/notifications/broadcast` | platform / org | `{ title(≤200), body(≤1000), send_email? }` | Requires tenant. Email requires that tenant's configured, enabled SMTP; no platform fallback. Fan-out to tenant users/agents |

Archive state is independent of read state: restoring an unread notice adds it back to
active unread counts. Category filters retain their existing overlapping membership
(for example, broadcast notices also appear in social). Agent unread consumption and
specialized notification deduplication are unchanged. The nullable archive column and
indexes belong to the fresh, rerunnable schema baseline; this change does not upgrade
an existing database.

### B10. Templates (market) - advanced router

**Router:** `app/api/advanced.py`

| Method | Path | Roles | Notes |
|--------|------|-------|-------|
| `DELETE` | `/api/templates/{template_id}` | admin | **204** |

(`POST /api/templates` is any authenticated user for “share to market”, not admin-only.)

### B11. OKR admin controls - `/api/okr/*`

**Router:** `app/api/okr.py` (self-prefixed)

| Method | Path | Roles | Request | Notes |
|--------|------|-------|---------|-------|
| `PUT` | `/api/okr/settings` | org_admin / platform_admin | `OKRSettingsUpdate`: enable flags, daily report time/skip non-workdays, period frequency/length | Period fields lock after first enable. May auto-seed OKR agent |
| `POST` | `/api/okr/sync-relationships` | org_admin / platform_admin | - | Rebuild OKR agent relationship graph |
| `POST` | `/api/okr/company-reports/regenerate` | org_admin / platform_admin | report type + period | Company report regen |
| `POST` | `/api/okr/trigger-daily-collection` | org_admin / platform_admin | - | Trigger daily collection |

Other OKR CRUD is broader than pure admin; some writes give admins extra scope.

---

## C. Admin-elevated capabilities on shared product APIs

These are **not** dedicated admin routers; admins receive wider access:

| Area | Router / path prefix | Behavior |
|------|----------------------|----------|
| Agents | `/api/agents/*` | Admins get `manage` on non-private company agents; may set `tenant_id` on create; elevated delete/start/stop/approvals |
| Agent files | `/api/agents/{id}/files/*` | Manage writes; `enterprise_info` paths restricted to admins |
| Agent credentials | `/api/agents/{id}/credentials` (credentials router) | platform/org can manage without creator-only access |
| Relationships | `/api/agents/{id}/relationships/*` | manage if admin or `manage` access |
| Chat sessions | `/api/agents/{id}/sessions` (self-prefixed) | platform / org / agent_admin / creator |
| Plaza | `/api/plaza/*` (self-prefixed) | platform cross-tenant; admins can delete posts |
| gogcli | `/api/agents/{id}/gogcli/*` | platform / org (or agent `manage`) for keyring/auth |
| Tool agent config | `/api/tools/agents/...` | only platform/org may set `allow_network` / proxy fields |
| Quotas | `quota_guard` service | platform/org often bypass member limits |

---

## D. Role matrix

| Capability | Platform admin | Tenant admin (`org_admin`) |
|------------|----------------|----------------------------|
| List / create / toggle all companies | Yes | No |
| Platform metrics & platform-settings flags | Yes | No |
| Change user role within their current company | Yes | No |
| Cross-tenant list/update (users, tools, IDPs, models via `tenant_id`) | Yes | Own tenant only |
| Company settings, logo, delete company | Any company | Own company |
| User list / quota / role | Any tenant (list); genesis PA may mint `platform_admin` | Own tenant; only genesis OA may mint `org_admin` |
| Create another platform admin | Genesis PA only | No |
| Create another org admin | No | Genesis OA only |
| Activate / deactivate other platform admins | Genesis PA only | No |
| Activate / deactivate other org admins | No | Genesis OA only |
| Activate / deactivate end users | Any tenant | Own tenant (`member` / `agent_admin`) |
| LLM pool, IDPs, invitations, KB, skills, tools catalog | Yes (global/tenant) | Own tenant |
| System setting key `platform` | Yes | **No** |
| Organization SMTP view/configure/delete/test | Active genesis PA, own tenant only | Active genesis OA, own tenant only |
| Broadcast / OKR company settings | With tenant context | Yes |

---

## E. Implementation anchors

| Concern | Path |
|---------|------|
| Role deps | `engine/app/core/security.py` |
| Platform console | `engine/app/api/admin.py` |
| Tenants | `engine/app/api/tenants.py` |
| Users | `engine/app/api/users.py`, `organization.py` |
| Enterprise / SSO / LLM / invites | `engine/app/api/enterprise.py` |
| Organization SMTP | `engine/app/api/tenant_email.py`, `app/schemas/tenant_email.py`, `app/services/tenant_email_admin.py`, `app/services/email_config.py` |
| SMTP delivery | `engine/app/services/system_email_service.py`, `engine/app/core/email.py`, `engine/app/core/smtp_policy.py` |
| Tools / skills / KB | `tools.py`, `skills.py`, `files.py` |
| OKR admin | `engine/app/api/okr.py` |
| Router mount map | `engine/app/main.py` (include_router block) |
| Shared Pydantic schemas | `engine/app/schemas/schemas.py` |
| API package notes | `engine/app/api/AGENTS.md` |

---

## F. Live OpenAPI

With the engine running:

```bash
# Swagger UI
open http://localhost:<port>/docs

# OpenAPI JSON
curl -s http://localhost:<port>/openapi.json | jq '.paths | keys'
```

Protected routes need a JWT from `/api/auth/login` or registration.

---

## G. Suggested use for `web-a` and follow-on work

1. **Platform surface** → primarily `/api/admin/*` + `GET /api/tenants/` + platform system settings (`key=platform`) + global identity providers.
2. **Tenant operator surface** → `/api/tenants/{id}`, `/api/users/*`, `/api/enterprise/*` (LLM, quotas, IDPs, invites), tools/skills catalogs, enterprise KB, broadcasts, OKR settings.
3. Prefer typed clients generated or hand-written against these contracts; keep secrets masked in UI (API already masks many keys).
4. When adding admin endpoints: put privileged handlers in the right router, use `require_role` / `get_current_admin`, enforce tenant isolation for org admins, update this doc.

---

## Changelog

| Date | Note |
|------|------|
| 2026-10-20 | Retired provider organization department/people sync and its authorization/verification routes; retained local departments, shared members, SSO and channels. Schema changes are fresh-only, with no existing-data cleanup or upgrade patches. |
| 2026-08-13 | Initial inventory from `engine/app/api` role gates and schemas |
