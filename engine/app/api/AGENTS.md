# app/api

Flat FastAPI routers. Most export `router` and are mounted from `app/main.py`.

## Registration

- Shared channel LLM helpers live in `app/services/channels/llm_bridge.py`. Import them directly from `app.services.channels.llm_bridge`, not from an API router.
- Default: `prefix=settings.API_PREFIX` so `/agents` → `/api/agents`.
- Routers that set their own prefix and mount without `API_PREFIX`: `triggers.py` and `chat_sessions.py` (`/api/agents`), `plaza.py` (`/api/plaza`), `webhooks.py` (`/api/webhooks`), `okr.py` (`/api/okr`), `linkup_proxy.py` (`/api/linkup`). `websocket.py` (`/ws/chat/{agent_id}`) and `pages.public_router` (`/p/{short_id}`) also mount without `API_PREFIX`, but set paths on route decorators rather than `APIRouter`.
- `files.py` exports `router`, `upload_router`, `enterprise_kb_router`.
- `whatsapp.py` is mounted with `API_PREFIX` (same as other IM channels). Webhook paths stay `/api/channel/whatsapp/{agent_id}/webhook`. No proactive outbound sender yet. `background_tasks.py` is a helper, not a router.
- Launch route background work with `schedule_background_task`, not `asyncio.create_task`. It starts the work after the request transaction commits, in a fresh context that carries only the trace id, and drops it on rollback. FastAPI `BackgroundTasks` still run before the request commits.
- `openclaw_updates.py` is mounted with `API_PREFIX` (`/admin/openclaw-updates` platform-admin publish, `/openclaw-updates/{id}`, `/agents/{id}/openclaw-update`). Guest-visible file/upload mutators carry `Depends(guest_write_exclusion)` (409 during an active run); `OpenClawUpdateGateError` maps to 423/409 in `main.py`. Contract: `../../docs/openclaw-updates.md`.
- `departments.py` is mounted with `API_PREFIX` (`/api/departments`, `PUT /api/users/{id}/departments`, and singular `/department`).

## Org departments

- `GET/POST /departments`: `org_admin` is own-tenant; `platform_admin` must pass `tenant_id`. Duplicate name → 409. Create locks in the router and does not bump ACL.
- `PUT /users/{id}/departments`: `org_departments.assign_departments`. `{department_ids: UUID[]}` replaces the full set, normalizes duplicates, and clears on `[]`; returns `{user_id, tenant_id, department_ids}` sorted by UUID. Invalid/foreign departments reject the entire set. End users only. Fresh actor/subject checks follow tenant-first locks; affected old/new agents are locked in UUID order and `aclver` is bumped in the same transaction.
- `PUT /users/{id}/department`: singular compatibility route via `assign_department`; non-null replaces with one membership, null clears all. `GET /departments` still lists one assignment row per membership.
- Provider organization directory/sync routes are retired. `/api/org/users` and org departments remain; neither requires a provider department hierarchy.
- `agents.py` stores `scope_type=department` at `access_level=use` only, and only for custom scope (other scopes with `department_ids` → 422). Saving private or company deletes every permission row, so those grants disappear. DB check: `ck_department_permission_use`. `GET /{agent_id}/permissions/departments` is managers-only.
- `POST /agents/` accepts optional `department_ids` (default `[]`). Nonempty selections require custom sharing; validate every department against the resolved target tenant before writes. Creator `manage` and deduplicated department `use` grants share the agent creation transaction through `AgentPermissionDAO.create`.

## Dependencies

- Users: `current_user: UserRecord = Depends(get_current_user)` - not `User`.
- Admin: `get_current_admin` / `require_role(...)` (both inherit `must_change_password` gate from `get_current_user`).
- Agents: `await check_agent_access(current_user, agent_id)`; no discarded DB argument is accepted.
- Persistence: DAOs. Short CRUD routers bind `bind_crud_connection` in `app.main` (one checkout). Do not attach that dependency to websocket / connector inbound / gateway / `linkup_proxy`. Extra `async with connection_ctx():` still joins.
- `agent_setup.py` is self-prefixed `/api/agents/{agent_id}/setup` and mounts without `API_PREFIX` or request-wide CRUD binding. Manage-only `GET` returns safe status; `POST /retry` accepts eligible failed setup with 202, otherwise 409. The service freshly authorizes, owns explicit transactions and rechecks authorization after acquiring the per-agent flock; never move row locks ahead of that flock.
- `admin_search_analytics.py`: platform_admin only. Live SQL over `web_search_events`. Never return raw queries.
- Handlers use canonical arguments without legacy DB placeholders; preserve real `DbConnection` parameters in transaction/locking helpers.
- `websocket.py` redeems single-use `chat_tickets` and freshly loads the bound principal; `files.py` download takes Bearer (`load_user_from_access_token`, identity + force-change) or a single-use `file_download_tickets` ticket from `POST .../files/download-ticket`, freshly re-authorized on redemption; no query JWT. `gateway.py` uses agent API keys.
- `gogcli.py` is mounted (`/api/agents/{id}/gogcli`); users are `UserRecord`.

## Auth / admin genesis

- Registration uses `POST /auth/register/init`; OAuth registration/login uses `POST /auth/{provider}/callback`. Fresh-only installs do not expose `/auth/register`, `/auth/register/sso`, or `/auth/email-hint`. Auth responses use `needs_org_confirm`, not `needs_company_setup`.
- `auth.py`: password login returns `must_change_password` on `TokenResponse` / `AuthUser` / `IdentityOut`. Open registration hard-codes `is_platform_admin=False` (never first-user elevation). Reset/verify emails resolve the frontend via `frontend_origin` (allowlisted Origin/Referer).
- `PUT /auth/me/password` uses `get_authenticated_user`; rejects `new_password == old_password`; clears `must_change_password`. Password reset also clears the flag.
- `admin.py`: `POST /companies` requires `name`, `admin_email`, `admin_password` (optional display name). Creates tenant + genesis `org_admin` with `must_change_password=True` via `tenant_provisioning`. Unique email race → 409. `POST /platform-admins` is **genesis platform admin only**. Genesis PA may `PATCH /platform-admins/{id}/active`. Admin trail: `GET /audit-logs`.
- `tenants.py`: `POST /` is platform-admin only and creates a tenant with its genesis org admin (same service as `POST /admin/companies`). Join always assigns `member`. Assign-user cannot set `org_admin`.
- `users.py`: `POST /org-admins` and promoting to `org_admin` are **genesis org admin only**. Promoting to `platform_admin` is **genesis platform admin only**. Genesis OA may `PATCH /org-admins/{id}/active`. PA/OA may `PATCH /{id}/active` for end users (also stops that user's agents/triggers/schedules). User detail includes `agents_count`. Company trail: `GET /admin-audit-logs`.
- Inventory for clients: `docs/admin-apis.md`.

## Catalog / secrets

- `tools.py`: tenant override is platform-admin only; catalog mutations need org/platform admin; mask company/agent secrets; `/test-mcp` uses the shared async public-destination policy, with connection-time enforcement in `MCPClient`.
- Tool ownership policy: `services/tool_ownership.py`, using `UserRecord.is_platform_admin` (role or identity). PA may mutate any catalog record. OA record writes require non-builtin source plus non-null record and caller tenant IDs that match; shared/foreign rows and tenantless OA are denied. Builtin source stays shared even if tenant-tagged. Other roles cannot mutate.
- Builtin config-only PUT may replace `tenant_settings` config for a concrete authorized PA/OA target (OA own tenant only). Non-builtin config is a row write requiring record rights. Config is a full replacement, never a merge. Metadata or mixed config + metadata requires record rights before any settings/DAO write; reject the whole forbidden request. Bulk preloads and authorizes all existing rows before any write, silently skipping missing IDs. Delete keeps `type=builtin` 400 and denies unowned rows before deleting assignments. Keep `get_current_user` force-password gating.
- Only catalog GET adds required `tenant_id: str | None` (stored owner) and `capabilities: {can_edit_details: bool, can_delete: bool, can_configure: bool}`. Details follows record rights; delete additionally excludes builtin type; config requires record rights for non-builtin source or a concrete authorized target for builtin source. Populate using the same policy and resolved target; member reads have all false. Existing keys and agent-installed/config response shapes stay unchanged. Exact matrix: `docs/admin-apis.md` B7.
- `update_agent_tool_config`: only `platform_admin` / `org_admin` may set `allow_network` or proxy fields.
- LLM pool writes (`/enterprise/llm-models`, `/llm-test`, `/llm-providers`) are org/platform admin only. Members may `GET /llm-models` for the enabled catalog (no keys or base URLs). Company primary is `POST .../set-default`; fallback is `POST .../set-fallback`. Agent `primary_model_id` / `fallback_model_id` writes are org/platform admin only. Helpers: `app.services.enterprise_llm`. End users and agents may use **only** their company's pool rows (`model_usable_in_tenant` rejects global and cross-tenant). Grok SuperGrok / X Premium device-code is `POST .../grok-subscription/start` + `GET .../grok-subscription/status`. ChatGPT Plus / Pro / Team browser PKCE is `POST .../chatgpt-subscription/start` → `{attempt_id, authorization_url, redirect_uri, expires_in}`, `POST .../complete` with strict `{attempt_id, callback_url}`, and read-only `GET .../status?attempt_id=...` (no provider call). Completion/status return `{status, attempt_id, model_id?, detail?, expires_in?}`; tokens and private verifier stay server-side. Refresh is `POST .../{id}/refresh-subscription`. Services: `grok_subscription` + `grok_oauth`; ChatGPT handoff/store/types/PKCE modules plus `chatgpt_subscription` + `chatgpt_oauth`. Keep enterprise unbound from request-wide CRUD transactions so the browser exchange never holds a DB checkout.

## Hotspots

- Large: `feishu.py` (event/file still in-router; LLM is `channels/llm_bridge.py`), `okr.py` (schemas + period math extracted; gap/outreach still here), `enterprise.py`, `auth.py`, `files.py`, `agentbay_control.py`, `websocket.py`, `agents.py`, `tools.py`, `skills.py`, `wecom.py`, `gateway.py`, `tenants.py`, `teams.py`, `admin.py`, `slack.py`.
- Split new domain logic out of those files. Do not expose secrets in response models.

## Agent deletion and ownership

- `DELETE /agents/{id}` is `platform_admin` only; everyone else (even the creator or an org admin) gets 403 `agent_deletion_requires_request`.
- `agent-deletion-requests.py` (`/api/agent-deletion-requests`): `POST` `{agent_id, reason}` (owner or org admin with manage; reason trimmed, 1..1000), `GET` list (`status`, `agent_id`, `limit`, `cursor` → `{items, next_cursor}`; members see their own, eligible org admins the company), `GET /{id}` (records an eligible org admin's view), `POST /{id}/approve` (idempotent; needs a prior view), `POST /{id}/cancel` (requester withdraw). Two distinct fresh-eligible org admins, or the active genesis org admin, execute through `snapshot_agent_teardown` + `agent_dao.delete_with_related` + `schedule_agent_teardown`. Stale owner/tenant/agent cancels the request (`cancel_reason`). Lock order: tenant → sorted users → agent → request.
- `POST /agent-ownership-transfers` `{from_user_id, to_user_id, agent_ids}`: active same-company org admin; source `member`/`agent_admin` (active or not), target active `member`/`agent_admin`; atomic, quota-checked, cancels pending deletion requests (`owner_changed`). `POST /agents/{id}/handover` reuses the same service (`services/agent_ownership_transfer.py`).
