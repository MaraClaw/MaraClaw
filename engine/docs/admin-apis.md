# Admin APIs — Platform admin & Tenant admin

**Audience:** Agents and humans building `web-a` (admin console), reviewing RBAC, or wiring clients against the engine.  
**Source of truth:** FastAPI routers under `engine/app/api/`, mounted with `API_PREFIX=/api` (`engine/app/config.py`, `engine/app/main.py`).  
**Generated from codebase review:** 2026-08-13 (branch work). Re-check routers if routes drift.

In product language:

| Product term | Code role | Scope |
|--------------|-----------|--------|
| **Platform admin** | `platform_admin` | Cross-tenant; platform metrics/settings; assign users to companies |
| **Tenant admin** | `org_admin` | Company admin for the caller’s `tenant_id` |

Also accepted in some gates: `identity.is_platform_admin` elevates like `platform_admin` when that role is allowed.

---

## Auth model

| Mechanism | Location | Behavior |
|-----------|----------|----------|
| Bearer JWT | `get_current_user` | `Authorization: Bearer <token>` |
| Shared admin | `get_current_admin` (`app/core/security.py`) | Requires `platform_admin` **or** `org_admin` (or identity platform flag) |
| Exact roles | `require_role("…")` | Exact match; identity platform flag counts if `platform_admin` is listed |
| Hierarchy | `ROLE_HIERARCHY` | `member` < `agent_admin` < `org_admin` < `platform_admin` |

First registered user becomes `platform_admin`. Other admins are assigned via role APIs or invite flows.

Base path for most routes: **`/api`**.

Self-prefixed exceptions (no double-prefix): `okr` → `/api/okr`, plus a few public/websocket routers (see `app/api/AGENTS.md`).

---

## A. Platform-admin only

### A1. Platform company console — `/api/admin/*`

**Router:** `app/api/admin.py`  
**Auth:** `require_role("platform_admin")` on every route.

| Method | Path | Request | Response / notes |
|--------|------|---------|------------------|
| `GET` | `/api/admin/companies` | — | `CompanyStats[]`: `id`, `name`, `slug`, `is_active`, `sso_enabled`, `sso_domain`, `created_at`, `user_count`, `agent_count`, `agent_running_count`, `total_tokens`, `cache_read_tokens_total`, `org_admin_email` |
| `POST` | `/api/admin/companies` | `{ name: string(1–200) }` | **201** `{ company: CompanyStats, admin_invitation_code: string }` — creates tenant + 1-use invite |
| `PUT` | `/api/admin/companies/{company_id}/toggle` | — | `{ ok, is_active }` — disables company and pauses running agents when turning off |
| `GET` | `/api/admin/metrics/timeseries` | Query: `start_date`, `end_date` (datetime) | Daily series: companies, users, tokens, cache, sessions, DAU/WAU/MAU, cache hit rate |
| `GET` | `/api/admin/metrics/leaderboards` | — | `{ top_companies[], top_agents[] }` (top 20 by tokens + cache stats) |
| `GET` | `/api/admin/metrics/enhanced` | — | avg tokens/session 30d, 7d retention, channel distribution, tool category top10, churn warnings |
| `GET` | `/api/admin/platform-settings` | — | `{ allow_self_create_company, invitation_code_enabled, sso_custom_domain_redirect_enabled }` |
| `PUT` | `/api/admin/platform-settings` | Same fields optional | Updated `PlatformSettingsOut` |

### A2. Tenants — platform-only pieces

**Router:** `app/api/tenants.py`

| Method | Path | Auth | Request | Response / notes |
|--------|------|------|---------|------------------|
| `GET` | `/api/tenants/` | platform_admin | — | `TenantOut[]` all tenants |
| `PUT` | `/api/tenants/{tenant_id}/assign-user/{user_id}` | platform_admin | Query: `role` ∈ `org_admin` \| `agent_admin` \| `member` (default `member`) | `{ status, user_id, tenant_id, role }` |

**SSO note:** On `PUT /api/tenants/{tenant_id}`, platform admins **cannot** set `sso_enabled` / `sso_domain` (stripped server-side). SSO is managed by the company’s own org admin via Enterprise settings / identity providers.

---

## B. Tenant admin + platform admin (shared)

Unless noted: both `org_admin` and `platform_admin` work. Platform may cross tenants (often via `tenant_id` query/body); org admin is limited to own `tenant_id`.

### B1. Tenants / company settings — `/api/tenants/*`

**Router:** `app/api/tenants.py`

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/tenants/{tenant_id}` | platform / org | — | Org: own tenant only. **Res:** `TenantOut` |
| `PUT` | `/api/tenants/{tenant_id}` | platform / org | `TenantUpdate`: optional `name`, `im_provider`, `timezone`, `country_region`, `is_active`, `sso_enabled`, `sso_domain`, `a2a_async_enabled` | Org: own tenant. Platform: SSO fields stripped |
| `POST` | `/api/tenants/{tenant_id}/logo` | platform / org | Multipart image PNG/JPEG/WebP, ≤1 MB, 1:1 square | `TenantOut` |
| `DELETE` | `/api/tenants/{tenant_id}/logo` | platform / org | — | `TenantOut` |
| `DELETE` | `/api/tenants/{tenant_id}` | own org_admin **or** platform | — | Cascade delete. `{ status: "deleted", fallback_tenant_id }` |

#### `TenantOut` shape

```text
id, name, slug, im_provider, timezone, country_region, is_active,
sso_enabled, sso_domain, a2a_async_enabled, default_model_id, logo_url, created_at
```

#### Related non-admin tenant routes (context only)

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/api/tenants/self-create` | Authenticated; creator becomes org_admin (flag-gated) |
| `POST` | `/api/tenants/join` | Invite code join |
| `GET` | `/api/tenants/registration-config` | Public |
| `GET` | `/api/tenants/resolve-by-domain` | Public SSO domain resolve |
| `GET` | `/api/tenants/me` | Any member; company + default model |
| `GET` | `/api/tenants/me/token-usage` | Any member; token/cache aggregates |
| `GET` | `/api/tenants/{tenant_id}/logo` | Public asset by UUID |

### B2. Users — `/api/users/*`

**Router:** `app/api/users.py`

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/users/` | platform / org | Query: `tenant_id?` (platform only) | `UserOut[]` with quotas + `agents_count` |
| `PATCH` | `/api/users/{user_id}/quota` | platform / org | `UserQuotaUpdate`: `quota_message_limit?`, `quota_message_period?` (`permanent`\|`daily`\|`weekly`\|`monthly`), `quota_max_agents?`, `quota_agent_ttl_hours?` | Same-tenant only. `UserOut` |
| `PATCH` | `/api/users/{user_id}/role` | platform / org | `{ role }` | Org may set `org_admin` \| `member`. Platform may also set `platform_admin`. Blocks demoting last admin |

#### `UserOut` (users router)

```text
id, username, email, display_name, role, is_active,
quota_message_limit, quota_message_period, quota_messages_used,
quota_max_agents, quota_agent_ttl_hours, agents_count, created_at, source
```

### B3. Org user profile — `/api/org/*`

**Router:** `app/api/organization.py`

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/org/users` | any auth; admins may pass `tenant_id` | Query: `tenant_id?` | Active users, display-name ordered |
| `PATCH` | `/api/org/users/{user_id}` | admin (`get_current_admin`) | `UserUpdate`: `username?`, `email?`, `display_name?`, `avatar_url?`, `title?`, `primary_mobile?` | Uniqueness checks; syncs org-member contact |

### B4. Enterprise suite — `/api/enterprise/*`

**Router:** `app/api/enterprise.py`  
Default admin gate: `get_current_admin` unless noted.

#### LLM pool

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/enterprise/llm-providers` | any auth | — | Provider registry manifest |
| `POST` | `/api/enterprise/llm-test` | admin | `provider`, `model`, `api_key?`, `base_url?`, `model_id?` | `{ success, latency_ms, reply? \| error? }` |
| `GET` | `/api/enterprise/llm-models` | auth | Query: `tenant_id?` | Non-platform cannot other tenants. Keys masked |
| `POST` | `/api/enterprise/llm-models` | admin | `LLMModelCreate` + Query `tenant_id?` | **201** `LLMModelOut` |
| `POST` | `/api/enterprise/llm-models/{model_id}/set-default` | admin | — | Sets tenant default; may migrate agents. **204** |
| `PUT` | `/api/enterprise/llm-models/{model_id}` | admin | `LLMModelUpdate` (partial) | `LLMModelOut` |
| `DELETE` | `/api/enterprise/llm-models/{model_id}` | admin | Query: `force?` | **409** if agents use model unless forced. **204** |

**`LLMModelCreate`:** `provider`, `model`, `api_key`, `base_url?`, `label`, `temperature?` (0–2), `max_tokens_per_day?`, `enabled`, `supports_vision`, `max_output_tokens?`, `request_timeout?`.

#### Enterprise info, approvals, audit, stats

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/enterprise/info` | auth | — | `EnterpriseInfoOut[]` |
| `PUT` | `/api/enterprise/info/{info_type}` | admin | `{ content: object, visible_roles: string[] }` | Upsert + sync to agents |
| `GET` | `/api/enterprise/approvals` | auth | Query: `tenant_id?`, `status_filter?` | Platform broader; others creator-scoped |
| `POST` | `/api/enterprise/approvals/{approval_id}/resolve` | auth | `{ action: "approve" \| "reject" }` | `ApprovalRequestOut` |
| `GET` | `/api/enterprise/audit-logs` | admin | Query: `agent_id?`, `tenant_id?`, `limit=50` | `AuditLogOut[]` |
| `GET` | `/api/enterprise/stats` | admin | Query: `tenant_id?` | `{ total_agents, running_agents, total_users, pending_approvals }` |

#### Tenant quotas (caller’s tenant)

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/enterprise/tenant-quotas` | auth + tenant | — | Default limits: messages, agents, TTL, LLM calls/day, heartbeat floor, triggers, poll/webhook ceilings |
| `PATCH` | `/api/enterprise/tenant-quotas` | admin | Same fields optional | May adjust existing agent heartbeats. `{ message, heartbeat_agents_adjusted }` |

#### System email & settings

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `POST` | `/api/enterprise/system-email/test` | admin | `{ email }` | SMTP probe |
| `GET` | `/api/enterprise/email-templates` | admin | — | templates + variables + defaults |
| `PUT` | `/api/enterprise/email-templates` | admin | `{ templates: object }` | Persist templates |
| `GET` | `/api/enterprise/system-settings/{key}` | auth | — | `{ key, value, updated_at? }` |
| `PUT` | `/api/enterprise/system-settings/{key}` | admin | `{ value: object }` | Key **`platform`**: **platform admin only**; may regenerate all SSO domains |
| `GET` | `/api/enterprise/system-settings/notification_bar/public` | public | — | Notification bar config |

#### Identity providers / SSO

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/enterprise/identity-providers` | auth | Query: `tenant_id?`, `global_only?` | `global_only` platform only. Secrets sanitized |
| `POST` | `/api/enterprise/identity-providers` | admin | `provider_type`, `name`, `is_active`, `sso_login_enabled`, `config`, `tenant_id?` | Org: own tenant. Platform may create global Google/GitHub without tenant |
| `POST` | `/api/enterprise/identity-providers/oauth2` | admin | OAuth2 fields: `app_id`, `app_secret`, URLs, `scope?` | Tenant required |
| `PATCH` | `/api/enterprise/identity-providers/{provider_id}/oauth2` | admin | Partial OAuth2 fields | Tenant ownership for non-platform |
| `PUT` | `/api/enterprise/identity-providers/{provider_id}` | admin | `name?`, `is_active?`, `sso_login_enabled?`, `config?` | Syncs tenant SSO state |
| `DELETE` | `/api/enterprise/identity-providers/{provider_id}` | admin | — | **204** |

#### Org directory

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/enterprise/org/departments` | auth | Query: `tenant_id?`, `provider_id?` | Platform without tenant can browse globally |
| `GET` | `/api/enterprise/org/members` | auth | Query: `department_id?`, `search?`, `tenant_id?`, `provider_id?` | Limit 100 |
| `POST` | `/api/enterprise/org/sync` | admin | Query: `provider_id` required | Triggers identity org sync |

Public WeCom verification (no admin JWT):  
`GET /api/enterprise/org/wecom-verify/{provider_id}`,  
`GET /api/enterprise/org/wecom-callback/{token}`.

#### Invitations (admin **and** caller must have `tenant_id`)

| Method | Path | Request | Response / notes |
|--------|------|---------|------------------|
| `POST` | `/api/enterprise/invitation-codes` | `{ count=1, max_uses=1 }` (count ≤ 100) | `{ created, codes[] }` |
| `POST` | `/api/enterprise/invite-users` | `{ emails: EmailStr[] }` | Needs system SMTP. `{ invited, message }` |
| `GET` | `/api/enterprise/invitation-codes` | Query: `page`, `page_size`, `search` | Paginated list |
| `GET` | `/api/enterprise/invitation-codes/export` | — | CSV download |
| `DELETE` | `/api/enterprise/invitation-codes/{code_id}` | — | Soft-deactivate |

### B5. Google Workspace admin helper

**Router:** `app/api/google_workspace.py`

| Method | Path | Roles | Response / notes |
|--------|------|-------|------------------|
| `GET` | `/api/enterprise/identity-providers/{provider_id}/google-workspace-sync/authorize-url` | admin | Tenant ownership for non-platform. `{ authorization_url }` |

### B6. Enterprise knowledge base — `/api/enterprise/knowledge-base/*`

**Router:** `enterprise_kb_router` in `app/api/files.py`

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/enterprise/knowledge-base/files` | auth + tenant | Query: `path?` | List dir entries |
| `POST` | `/api/enterprise/knowledge-base/upload` | **admin** | Multipart `file`, `sub_path?` | May auto-extract text from binaries |
| `GET` | `/api/enterprise/knowledge-base/content` | auth | Query: `path` | File content |
| `PUT` | `/api/enterprise/knowledge-base/content` | **admin** | Query: `path` + body content | Write file |
| `DELETE` | `/api/enterprise/knowledge-base/content` | **admin** | Query: `path` | File or tree delete |

### B7. Tools catalog — `/api/tools/*`

**Router:** `app/api/tools.py`  
Catalog mutations: `_require_catalog_manager` → `platform_admin` \| `org_admin`.  
Platform may target another company via `tenant_id`.

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `GET` | `/api/tools` | auth | Query: `tenant_id?` | Tenant-scoped catalog; secrets masked |
| `POST` | `/api/tools` | admin | `ToolCreate`: name, display_name, description, type, category, icon, schemas, MCP fields, `is_default`, `tenant_id?` | `{ id, name }` |
| `PUT` | `/api/tools/bulk` | admin | `[{ tool_id, enabled }]` | `{ ok: true }` |
| `PUT` | `/api/tools/{tool_id}` | admin | `ToolUpdate` (partial; company config for builtins) | `{ ok: true }` |
| `DELETE` | `/api/tools/{tool_id}` | admin | — | Non-builtin only; tenant ownership for non-platform |

Agent-level tool assignment/config routes exist under `/api/tools/agents/...` but use agent access checks (not pure admin gates). **Only** platform/org may set `allow_network` / proxy fields on agent tool config.

### B8. Skills — `/api/skills/*`

**Router:** `app/api/skills.py`

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `POST` | `/api/skills/` | admin | `name`, `description`, `category`, `icon`, `folder_name`, `files[{path,content}]` | Create skill |
| `PUT` | `/api/skills/{skill_id}` | admin | partial skill update | Platform: all. Org: own-tenant + builtins |
| `DELETE` | `/api/skills/{skill_id}` | admin | — | Same write rules |
| `GET` | `/api/skills/settings/token` | org_admin / platform_admin | — | GitHub/ClawHub status (masked) |
| `PUT` | `/api/skills/settings/token` | org_admin / platform_admin | `{ github_token?, clawhub_key? }` | Requires tenant |
| `PUT` | `/api/skills/browse/write` | admin | `{ path, content }` | Path-based skill file write |
| `DELETE` | `/api/skills/browse/delete` | admin | Query: `path` | Delete file or whole skill folder |

### B9. Notifications broadcast

**Router:** `app/api/notification.py`

| Method | Path | Roles | Request | Response / notes |
|--------|------|-------|---------|------------------|
| `POST` | `/api/notifications/broadcast` | platform / org | `{ title(≤200), body(≤1000), send_email? }` | Requires tenant. Email needs system SMTP. Fan-out to tenant users/agents |

### B10. Templates (market) — advanced router

**Router:** `app/api/advanced.py`

| Method | Path | Roles | Notes |
|--------|------|-------|-------|
| `DELETE` | `/api/templates/{template_id}` | admin | **204** |

(`POST /api/templates` is any authenticated user for “share to market”, not admin-only.)

### B11. OKR admin controls — `/api/okr/*`

**Router:** `app/api/okr.py` (self-prefixed)

| Method | Path | Roles | Request | Notes |
|--------|------|-------|---------|-------|
| `PUT` | `/api/okr/settings` | org_admin / platform_admin | `OKRSettingsUpdate`: enable flags, daily report time/skip non-workdays, period frequency/length | Period fields lock after first enable. May auto-seed OKR agent |
| `POST` | `/api/okr/sync-relationships` | org_admin / platform_admin | — | Rebuild OKR agent relationship graph |
| `POST` | `/api/okr/company-reports/regenerate` | org_admin / platform_admin | report type + period | Company report regen |
| `POST` | `/api/okr/trigger-daily-collection` | org_admin / platform_admin | — | Trigger daily collection |

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
| Assign user to any tenant | Yes | No |
| Cross-tenant list/update (users, tools, IDPs, models via `tenant_id`) | Yes | Own tenant only |
| Company settings, logo, delete company | Any company | Own company |
| User list / quota / role | Any tenant (list/role) | Own tenant |
| LLM pool, IDPs, invitations, KB, skills, tools catalog | Yes (global/tenant) | Own tenant |
| System setting key `platform` | Yes | **No** |
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
| 2026-08-13 | Initial inventory from `engine/app/api` role gates and schemas |
